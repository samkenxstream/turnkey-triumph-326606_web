'''
    Copyright (C) 2017 Gitcoin Core

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program. If not, see <http://www.gnu.org/licenses/>.

'''
# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import collections
import logging
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.contrib.humanize.templatetags.humanize import naturalday, naturaltime
from django.contrib.postgres.fields import ArrayField, JSONField
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.db import models
from django.db.models import Q, Sum
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver
from django.urls import reverse
from django.urls.exceptions import NoReverseMatch
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

import pytz
import requests
from dashboard.tokens import addr_to_token
from economy.models import SuperModel
from economy.utils import ConversionRateNotFoundError, convert_amount, convert_token_to_usdt
from gas.utils import recommend_min_gas_price_to_confirm_in_time
from git.utils import (
    _AUTH, HEADERS, TOKEN_URL, build_auth_dict, get_gh_issue_details, get_gh_issue_state, get_issue_comments,
    get_url_dict, issue_number, org_name, repo_name,
)
from marketing.models import LeaderboardRank
from rest_framework import serializers
from web3 import Web3

from .signals import m2m_changed_interested

logger = logging.getLogger(__name__)


class BountyQuerySet(models.QuerySet):
    """Handle the manager queryset for Bounties."""

    def current(self):
        """Filter results down to current bounties only."""
        return self.filter(current_bounty=True, admin_override_and_hide=False)

    def stats_eligible(self):
        """Exclude results that we don't want to track in statistics."""
        return self.exclude(current_bounty=True, idx_status__in=['unknown', 'cancelled'])

    def exclude_by_status(self, excluded_statuses=None):
        """Exclude results with a status matching the provided list."""
        if excluded_statuses is None:
            excluded_statuses = []

        return self.exclude(idx_status__in=excluded_statuses)

    def filter_by_status(self, filtered_status=None):
        """Filter results with a status matching the provided list."""
        if filtered_status is None:
            filtered_status = list()
        elif isinstance(filtered_status, list):
            return self.filter(idx_status__in=filtered_status)
        else:
            return

    def keyword(self, keyword):
        """Filter results to all Bounty objects containing the keywords.

        Args:
            keyword (str): The keyword to search title, issue description, and issue keywords by.

        Returns:
            dashboard.models.BountyQuerySet: The QuerySet of bounties filtered by keyword.

        """
        return self.filter(
            Q(metadata__issueKeywords__icontains=keyword) | \
            Q(title__icontains=keyword) | \
            Q(issue_description__icontains=keyword)
        )

    def hidden(self):
        """Filter results to only bounties that have been manually hidden by moderators."""
        return self.filter(admin_override_and_hide=True)

    def visible(self):
        """Filter results to only bounties not marked as hidden."""
        return self.filter(admin_override_and_hide=False)

    def needs_review(self):
        """Filter results by bounties that need reviewed."""
        return self.prefetch_related('activities') \
            .filter(
                activities__activity_type__in=['bounty_abandonment_escalation_to_mods', 'bounty_abandonment_warning'],
                activities__needs_review=True,
            )

    def reviewed(self):
        """Filter results by bounties that have been reviewed."""
        return self.prefetch_related('activities') \
            .filter(
                activities__activity_type__in=['bounty_abandonment_escalation_to_mods', 'bounty_abandonment_warning'],
                activities__needs_review=False,
            )

    def warned(self):
        """Filter results by bounties that have been warned for inactivity."""
        return self.prefetch_related('activities') \
            .filter(
                activities__activity_type='bounty_abandonment_warning',
                activities__needs_review=True,
            )

    def escalated(self):
        """Filter results by bounties that have been escalated for review."""
        return self.prefetch_related('activities') \
            .filter(
                activities__activity_type='bounty_abandonment_escalation_to_mods',
                activities__needs_review=True,
            )

    def closed(self):
        """Filter results by bounties that have been closed on Github."""
        return self.filter(github_issue_details__state='closed')

    def not_started(self):
        """Filter results by bounties that have not been picked up in 3+ days."""
        dt = timezone.now() - timedelta(days=3)
        return self.prefetch_related('interested').filter(interested__isnull=True, created_on__gt=dt)


class Bounty(SuperModel):
    """Define the structure of a Bounty.

    Attributes:
        BOUNTY_TYPES (list of tuples): The valid bounty types.
        EXPERIENCE_LEVELS (list of tuples): The valid experience levels.
        PROJECT_LENGTHS (list of tuples): The possible project lengths.
        STATUS_CHOICES (list of tuples): The valid status stages.
        OPEN_STATUSES (list of str): The list of status types considered open.
        CLOSED_STATUSES (list of str): The list of status types considered closed.
        TERMINAL_STATUSES (list of str): The list of status types considered terminal states.

    """

    PERMISSION_TYPES = [
        ('permissionless', 'permissionless'),
        ('approval', 'approval'),
    ]
    PROJECT_TYPES = [
        ('traditional', 'traditional'),
        ('contest', 'contest'),
        ('cooperative', 'cooperative'),
    ]
    BOUNTY_TYPES = [
        ('Bug', 'Bug'),
        ('Security', 'Security'),
        ('Feature', 'Feature'),
        ('Unknown', 'Unknown'),
    ]
    EXPERIENCE_LEVELS = [
        ('Beginner', 'Beginner'),
        ('Intermediate', 'Intermediate'),
        ('Advanced', 'Advanced'),
        ('Unknown', 'Unknown'),
    ]
    PROJECT_LENGTHS = [
        ('Hours', 'Hours'),
        ('Days', 'Days'),
        ('Weeks', 'Weeks'),
        ('Months', 'Months'),
        ('Unknown', 'Unknown'),
    ]

    STATUS_CHOICES = (
        ('cancelled', 'cancelled'),
        ('done', 'done'),
        ('expired', 'expired'),
        ('open', 'open'),
        ('started', 'started'),
        ('submitted', 'submitted'),
        ('unknown', 'unknown'),
    )
    OPEN_STATUSES = ['open', 'started', 'submitted']
    CLOSED_STATUSES = ['expired', 'unknown', 'cancelled', 'done']
    TERMINAL_STATUSES = ['done', 'expired', 'cancelled']

    web3_type = models.CharField(max_length=50, default='bounties_network')
    title = models.CharField(max_length=255)
    web3_created = models.DateTimeField(db_index=True)
    value_in_token = models.DecimalField(default=1, decimal_places=2, max_digits=50)
    token_name = models.CharField(max_length=50)
    token_address = models.CharField(max_length=50)
    bounty_type = models.CharField(max_length=50, choices=BOUNTY_TYPES, blank=True)
    project_length = models.CharField(max_length=50, choices=PROJECT_LENGTHS, blank=True)
    experience_level = models.CharField(max_length=50, choices=EXPERIENCE_LEVELS, blank=True)
    github_url = models.URLField(db_index=True)
    github_issue_details = JSONField(default={}, blank=True, null=True)
    github_comments = models.IntegerField(default=0)
    bounty_owner_address = models.CharField(max_length=50)
    bounty_owner_email = models.CharField(max_length=255, blank=True)
    bounty_owner_github_username = models.CharField(max_length=255, blank=True)
    bounty_owner_name = models.CharField(max_length=255, blank=True)
    bounty_owner_profile = models.ForeignKey(
        'dashboard.Profile', null=True, on_delete=models.SET_NULL, related_name='bounties_funded', blank=True
    )
    is_open = models.BooleanField(help_text=_('Whether the bounty is still open for fulfillments.'))
    expires_date = models.DateTimeField()
    raw_data = JSONField()
    metadata = JSONField(default={}, blank=True)
    current_bounty = models.BooleanField(
        default=False, help_text=_('Whether this bounty is the most current revision one or not'))
    _val_usd_db = models.DecimalField(default=0, decimal_places=2, max_digits=50)
    contract_address = models.CharField(max_length=50, default='')
    network = models.CharField(max_length=255, blank=True, db_index=True)
    idx_experience_level = models.IntegerField(default=0, db_index=True)
    idx_project_length = models.IntegerField(default=0, db_index=True)
    idx_status = models.CharField(max_length=9, choices=STATUS_CHOICES, default='open', db_index=True)
    issue_description = models.TextField(default='', blank=True)
    standard_bounties_id = models.IntegerField(default=0)
    num_fulfillments = models.IntegerField(default=0)
    balance = models.DecimalField(default=0, decimal_places=2, max_digits=50)
    accepted = models.BooleanField(default=False, help_text=_('Whether the bounty has been done'))
    interested = models.ManyToManyField('dashboard.Interest', blank=True)
    interested_comment = models.IntegerField(null=True, blank=True)
    submissions_comment = models.IntegerField(null=True, blank=True)
    override_status = models.CharField(max_length=255, blank=True)
    last_comment_date = models.DateTimeField(null=True, blank=True)
    fulfillment_accepted_on = models.DateTimeField(null=True, blank=True)
    fulfillment_submitted_on = models.DateTimeField(null=True, blank=True)
    fulfillment_started_on = models.DateTimeField(null=True, blank=True)
    canceled_on = models.DateTimeField(null=True, blank=True)
    project_type = models.CharField(max_length=50, choices=PROJECT_TYPES, default='traditional')
    permission_type = models.CharField(max_length=50, choices=PERMISSION_TYPES, default='permissionless')
    snooze_warnings_for_days = models.IntegerField(default=0)

    token_value_time_peg = models.DateTimeField(blank=True, null=True)
    token_value_in_usdt = models.DecimalField(default=0, decimal_places=2, max_digits=50, blank=True, null=True)
    value_in_usdt_now = models.DecimalField(default=0, decimal_places=2, max_digits=50, blank=True, null=True)
    value_in_usdt = models.DecimalField(default=0, decimal_places=2, max_digits=50, blank=True, null=True)
    value_in_eth = models.DecimalField(default=0, decimal_places=2, max_digits=50, blank=True, null=True)
    value_true = models.DecimalField(default=0, decimal_places=2, max_digits=50, blank=True, null=True)
    privacy_preferences = JSONField(default={}, blank=True)
    admin_override_and_hide = models.BooleanField(
        default=False, help_text=_('Admin override to hide the bounty from the system')
    )
    admin_override_suspend_auto_approval = models.BooleanField(
        default=False, help_text=_('Admin override to suspend work auto approvals')
    )
    admin_mark_as_remarket_ready = models.BooleanField(
        default=False, help_text=_('Admin override to mark as remarketing ready')
    )
    attached_job_description = models.URLField(blank=True, null=True)

    # Bounty QuerySet Manager
    objects = BountyQuerySet.as_manager()

    class Meta:
        """Define metadata associated with Bounty."""

        verbose_name_plural = 'Bounties'
        index_together = [
            ["network", "idx_status"],
        ]

    def __str__(self):
        """Return the string representation of a Bounty."""
        return f"{'(CURRENT) ' if self.current_bounty else ''}{self.title} {self.value_in_token} " \
               f"{self.token_name} {self.web3_created}"

    def save(self, *args, **kwargs):
        """Define custom handling for saving bounties."""
        from .utils import clean_bounty_url
        if self.bounty_owner_github_username:
            self.bounty_owner_github_username = self.bounty_owner_github_username.lstrip('@')
        if self.github_url:
            self.github_url = clean_bounty_url(self.github_url)
            # issue_kwargs = get_url_dict(self.github_url)
            _org_name = org_name(self.github_url)
            _repo_name = repo_name(self.github_url)
            _issue_num = issue_number(self.github_url)
            # try:
            #     self.github_issue_details = get_gh_issue_details(_org_name, _repo_name, int(_issue_num))
            # except Exception as e:
            #     logger.error(e)
        super().save(*args, **kwargs)

    @property
    def profile_pairs(self):
        profile_handles = []

        for profile in self.interested.select_related('profile').all().order_by('pk'):
            profile_handles.append((profile.profile.handle, profile.profile.absolute_url))

        return profile_handles

    def get_absolute_url(self):
        """Get the absolute URL for the Bounty.

        Returns:
            str: The absolute URL for the Bounty.

        """
        return settings.BASE_URL + self.get_relative_url(preceding_slash=False)

    def get_relative_url(self, preceding_slash=True):
        """Get the relative URL for the Bounty.

        Attributes:
            preceding_slash (bool): Whether or not to include a preceding slash.

        Returns:
            str: The relative URL for the Bounty.

        """
        try:
            _org_name = org_name(self.github_url)
            _issue_num = int(issue_number(self.github_url))
            _repo_name = repo_name(self.github_url)
            return f"{'/' if preceding_slash else ''}issue/{_org_name}/{_repo_name}/{_issue_num}/{self.standard_bounties_id}"
        except Exception:
            return f"{'/' if preceding_slash else ''}funding/details?url={self.github_url}"

    def get_natural_value(self):
        token = addr_to_token(self.token_address)
        if not token:
            return 0
        decimals = token.get('decimals', 0)
        return float(self.value_in_token) / 10**decimals

    @property
    def url(self):
        return self.get_absolute_url()

    def snooze_url(self, num_days):
        """Get the bounty snooze URL.

        Args:
            num_days (int): The number of days to snooze the Bounty.

        Returns:
            str: The snooze URL based on the provided number of days.

        """
        return f'{self.get_absolute_url()}?snooze={num_days}'

    def approve_worker_url(self, worker):
        """Get the bounty work approval URL.

        Args:
            worker (string): The handle to approve

        Returns:
            str: The work approve URL based on the worker name

        """
        return f'{self.get_absolute_url()}?mutate_worker_action=approve&worker={worker}'

    def reject_worker_url(self, worker):
        """Get the bounty work rejection URL.

        Args:
            worker (string): The handle to reject

        Returns:
            str: The work reject URL based on the worker name

        """
        return f'{self.get_absolute_url()}?mutate_worker_action=reject&worker={worker}'

    @property
    def can_submit_after_expiration_date(self):
        if self.is_legacy:
            # legacy bounties could submit after expiration date
            return True

        # standardbounties
        contract_deadline = self.raw_data.get('contract_deadline')
        ipfs_deadline = self.raw_data.get('ipfs_deadline')
        if not ipfs_deadline:
            # if theres no expiry date in the payload, then expiration date is not mocked, and one cannot submit after expiration date
            return False

        # if contract_deadline > ipfs_deadline, then by definition, can be submitted after expiry date
        return contract_deadline > ipfs_deadline

    @property
    def title_or_desc(self):
        """Return the title of the issue."""
        if not self.title:
            title = self.fetch_issue_item('title') or self.github_url
            return title
        return self.title

    @property
    def issue_description_text(self):
        import re
        tag_re = re.compile(r'(<!--.*?-->|<[^>]*>)')
        return tag_re.sub('', self.issue_description).strip()

    @property
    def github_issue_number(self):
        try:
            return int(issue_number(self.github_url))
        except Exception:
            return None

    @property
    def org_name(self):
        return self.github_org_name

    @property
    def github_org_name(self):
        try:
            return org_name(self.github_url)
        except Exception:
            return None

    @property
    def github_repo_name(self):
        try:
            return repo_name(self.github_url)
        except Exception:
            return None

    def is_hunter(self, handle):
        """Determine whether or not the profile is the bounty hunter.

        Args:
            handle (str): The profile handle to be compared.

        Returns:
            bool: Whether or not the user is the bounty hunter.

        """
        return any(profile.fulfiller_github_username == handle for profile in self.fulfillments.all())

    def is_funder(self, handle):
        """Determine whether or not the profile is the bounty funder.

        Args:
            handle (str): The profile handle to be compared.

        Returns:
            bool: Whether or not the user is the bounty funder.

        """
        return handle.lower().lstrip('@') == self.bounty_owner_github_username.lower().lstrip('@')

    @property
    def absolute_url(self):
        return self.get_absolute_url()

    @property
    def avatar_url(self):
        return self.get_avatar_url(False)

    @property
    def avatar_url_w_gitcoin_logo(self):
        return self.get_avatar_url(True)

    def get_avatar_url(self, gitcoin_logo_flag=False):
        """Return the local avatar URL."""
        org_name = self.github_org_name
        gitcoin_logo_flag = "/1" if gitcoin_logo_flag else ""
        if org_name:
            return f"{settings.BASE_URL}static/avatar/{org_name}{gitcoin_logo_flag}"
        return f"{settings.BASE_URL}funding/avatar?repo={self.github_url}&v=3"

    @property
    def keywords(self):
        try:
            return self.metadata.get('issueKeywords', False)
        except Exception:
            return False

    @property
    def keywords_list(self):
        keywords = self.keywords
        if not keywords:
            return []
        else:
            try:
                return [keyword.strip() for keyword in keywords.split(",")]
            except AttributeError:
                return []

    @property
    def now(self):
        """Return the time now in the current timezone."""
        return timezone.now()

    @property
    def past_expiration_date(self):
        """Return true IFF issue is past expiration date"""
        return timezone.localtime().replace(tzinfo=None) > self.expires_date.replace(tzinfo=None)

    @property
    def past_hard_expiration_date(self):
        """Return true IFF issue is past smart contract expiration date
        and therefore cannot ever be claimed again"""
        return self.past_expiration_date and not self.can_submit_after_expiration_date

    @property
    def status(self):
        """Determine the status of the Bounty.

        Raises:
            Exception: Catch whether or not any exception is encountered and
                return unknown for status.

        Returns:
            str: The status of the Bounty.

        """
        if self.override_status:
            return self.override_status
        if self.is_legacy:
            return self.idx_status

        # standard bounties
        try:
            if not self.is_open:
                if self.accepted:
                    return 'done'
                elif self.past_hard_expiration_date:
                    return 'expired'
                has_tips = self.tips.filter(is_for_bounty_fulfiller=False).exclude(txid='').exists()
                if has_tips:
                    return 'done'
                # If its not expired or done, and no tips, it must be cancelled.
                return 'cancelled'
            # per https://github.com/gitcoinco/web/pull/1098 ,
            # cooperative/contest are open no matter how much started/submitted work they have
            if self.pk and self.project_type in ['contest', 'cooperative']:
                return 'open'
            if self.num_fulfillments == 0:
                if self.pk and self.interested.filter(pending=False).exists():
                    return 'started'
                return 'open'
            return 'submitted'
        except Exception as e:
            logger.warning(e)
            return 'unknown'

    @property
    def get_value_true(self):
        return self.get_natural_value()

    @property
    def get_value_in_eth(self):
        if self.token_name == 'ETH':
            return self.value_in_token
        try:
            return convert_amount(self.value_in_token, self.token_name, 'ETH')
        except Exception:
            return None

    @property
    def get_value_in_usdt_now(self):
        decimals = 10**18
        if self.token_name == 'USDT':
            return float(self.value_in_token)
        if self.token_name == 'DAI':
            return float(self.value_in_token / 10**18)
        try:
            return round(float(convert_amount(self.value_in_token, self.token_name, 'USDT')) / decimals, 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def get_value_in_usdt(self):
        if self.status in self.OPEN_STATUSES:
            return self.value_in_usdt_now
        return self.value_in_usdt_then

    @property
    def value_in_usdt_then(self):
        decimals = 10 ** 18
        if self.token_name == 'USDT':
            return float(self.value_in_token)
        if self.token_name == 'DAI':
            return float(self.value_in_token / 10 ** 18)
        try:
            return round(float(convert_amount(self.value_in_token, self.token_name, 'USDT', self.web3_created)) / decimals, 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def token_value_in_usdt_now(self):
        try:
            return round(convert_token_to_usdt(self.token_name), 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def token_value_in_usdt_then(self):
        try:
            return round(convert_token_to_usdt(self.token_name, self.web3_created), 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def get_token_value_in_usdt(self):
        if self.status in self.OPEN_STATUSES:
            return self.token_value_in_usdt_now
        return self.token_value_in_usdt_then

    @property
    def get_token_value_time_peg(self):
        if self.status in self.OPEN_STATUSES:
            return timezone.now()
        return self.web3_created

    @property
    def desc(self):
        return f"{naturaltime(self.web3_created)} {self.idx_project_length} {self.bounty_type} {self.experience_level}"

    @property
    def turnaround_time_accepted(self):
        try:
            return (self.get_fulfillment_accepted_on - self.web3_created).total_seconds()
        except Exception:
            return None

    @property
    def turnaround_time_started(self):
        try:
            return (self.get_fulfillment_started_on - self.web3_created).total_seconds()
        except Exception:
            return None

    @property
    def turnaround_time_submitted(self):
        try:
            return (self.get_fulfillment_submitted_on - self.web3_created).total_seconds()
        except Exception:
            return None

    @property
    def get_fulfillment_accepted_on(self):
        try:
            return self.fulfillments.filter(accepted=True).first().accepted_on
        except Exception:
            return None

    @property
    def get_fulfillment_submitted_on(self):
        try:
            return self.fulfillments.first().created_on
        except Exception:
            return None

    @property
    def get_fulfillment_started_on(self):
        try:
            return self.interested.first().created
        except Exception:
            return None

    @property
    def hourly_rate(self):
        try:
            hours_worked = self.fulfillments.filter(accepted=True).first().fulfiller_hours_worked
            return float(self.value_in_usdt) / float(hours_worked)
        except Exception:
            return None

    @property
    def is_legacy(self):
        """Determine if the Bounty is legacy based on sunset date.

        Todo:
            * Remove this method following legacy bounty sunsetting.

        Returns:
            bool: Whether or not the Bounty is using the legacy contract.

        """
        return (self.web3_type == 'legacy_gitcoin')

    def get_github_api_url(self):
        """Get the Github API URL associated with the bounty.

        Returns:
            str: The Github API URL associated with the issue.

        """
        from urllib.parse import urlparse
        if self.github_url.lower()[:19] != 'https://github.com/':
            return ''
        url_path = urlparse(self.github_url).path
        return 'https://api.github.com/repos' + url_path

    def fetch_issue_item(self, item_type='body'):
        """Fetch the item type of an issue.

        Args:
            type (str): The github API response body item to be fetched.

        Returns:
            str: The item content.

        """
        github_url = self.get_github_api_url()
        if github_url:
            issue_description = requests.get(github_url, auth=_AUTH)
            if issue_description.status_code == 200:
                item = issue_description.json().get(item_type, '')
                if item_type == 'body' and item:
                    self.issue_description = item
                elif item_type == 'title' and item:
                    self.title = item
                self.save()
                return item
        return ''

    def fetch_issue_comments(self, save=True):
        """Fetch issue comments for the associated Github issue.

        Args:
            save (bool): Whether or not to save the Bounty after fetching.

        Returns:
            dict: The comments data dictionary provided by Github.

        """
        if self.github_url.lower()[:19] != 'https://github.com/':
            return []

        parsed_url = urlsplit(self.github_url)
        try:
            github_user, github_repo, _, github_issue = parsed_url.path.split('/')[1:5]
        except ValueError:
            logger.info(f'Invalid github url for Bounty: {self.pk} -- {self.github_url}')
            return []
        comments = get_issue_comments(github_user, github_repo, github_issue)
        if isinstance(comments, dict) and comments.get('message', '') == 'Not Found':
            logger.info(f'Bounty {self.pk} contains an invalid github url {self.github_url}')
            return []
        comment_count = 0
        for comment in comments:
            if (isinstance(comment, dict) and comment.get('user', {}).get('login', '') not in settings.IGNORE_COMMENTS_FROM):
                comment_count += 1
        self.github_comments = comment_count
        if comment_count:
            comment_times = [datetime.strptime(comment['created_at'], '%Y-%m-%dT%H:%M:%SZ') for comment in comments]
            max_comment_time = max(comment_times)
            max_comment_time = max_comment_time.replace(tzinfo=pytz.utc)
            self.last_comment_date = max_comment_time
        if save:
            self.save()
        return comments

    @property
    def next_bounty(self):
        if self.current_bounty:
            return None
        try:
            return Bounty.objects.filter(standard_bounties_id=self.standard_bounties_id, created_on__gt=self.created_on).order_by('created_on').first()
        except Exception:
            return None

    @property
    def prev_bounty(self):
        try:
            return Bounty.objects.filter(standard_bounties_id=self.standard_bounties_id, created_on__lt=self.created_on).order_by('-created_on').first()
        except Exception:
            return None

    # returns true if this bounty was active at _time
    def was_active_at(self, _time):
        if _time < self.web3_created:
            return False
        if _time < self.created_on:
            return False
        next_bounty = self.next_bounty
        if next_bounty is None:
            return True
        if next_bounty.created_on > _time:
            return True
        return False

    def action_urls(self):
        """Provide URLs for bounty related actions.

        Returns:
            dict: A dictionary of action URLS for this bounty.

        """
        params = f'pk={self.pk}&network={self.network}'
        urls = {}
        for item in ['fulfill', 'increase', 'accept', 'cancel', 'payout', 'contribute', 'advanced_payout', 'social_contribution']:
            urls.update({item: f'/issue/{item}?{params}'})
        return urls

    def is_notification_eligible(self, var_to_check=True):
        """Determine whether or not a notification is eligible for transmission outside of production.

        Returns:
            bool: Whether or not the Bounty is eligible for outbound notifications.

        """
        if not var_to_check or self.get_natural_value() < 0.0001 or (
           self.network != settings.ENABLE_NOTIFICATIONS_ON_NETWORK):
            return False
        if self.network == 'mainnet' and (settings.DEBUG or settings.ENV != 'prod'):
            return False
        if (settings.DEBUG or settings.ENV != 'prod') and settings.GITHUB_API_USER != self.github_org_name:
            return False

        return True

    @property
    def is_project_type_fulfilled(self):
        """Determine whether or not the Project Type is currently fulfilled.

        Todo:
            * Add remaining Project Type fulfillment handling.

        Returns:
            bool: Whether or not the Bounty Project Type is fully staffed.

        """
        fulfilled = False
        if self.project_type == 'traditional':
            fulfilled = self.interested.filter(pending=False).exists()
        return fulfilled

    @property
    def needs_review(self):
        if self.activities.filter(needs_review=True).exists():
            return True
        return False

    # @property
    # def github_issue_state(self):
    #     _org_name = org_name(self.github_url)
    #     _repo_name = repo_name(self.github_url)
    #     _issue_num = issue_number(self.github_url)
    #     gh_issue_state = get_gh_issue_state(_org_name, _repo_name, int(_issue_num))
    #     return gh_issue_state

    # @property
    # def is_issue_closed(self):
    #     if self.github_issue_state == 'closed':
    #         return True
    #     return False

    @property
    def tips(self):
        """Return the tips associated with this bounty."""
        try:
            return Tip.objects.filter(github_url__iexact=self.github_url, network=self.network).order_by('-created_on')
        except:
            return Tip.objects.none()

    @property
    def bulk_payout_tips(self):
        """Return the Bulk payout tips associated with this bounty."""
        queryset = self.tips.filter(is_for_bounty_fulfiller=False, metadata__is_clone__isnull=True, metadata__direct_address__isnull=True)
        return (queryset.filter(from_address=self.bounty_owner_address) |
                queryset.filter(from_name=self.bounty_owner_github_username))

    @property
    def additional_funding_summary(self):
        """Return a dict describing the additional funding from crowdfunding that this object has"""
        return_dict = {
            'tokens': {},
            'usd_value': 0,
        }
        for tip in self.tips.filter(is_for_bounty_fulfiller=True):
            key = tip.tokenName
            if key not in return_dict['tokens'].keys():
                return_dict['tokens'][key] = 0
            return_dict['tokens'][key] += tip.amount_in_whole_units
            return_dict['usd_value'] += tip.value_in_usdt if tip.value_in_usdt else 0
        return return_dict

    @property
    def additional_funding_summary_sentence(self):
        afs = self.additional_funding_summary
        if len(afs['tokens'].keys()) == 0:
            return ""
        items = []
        for token, value in afs['tokens'].items():
            items.append(f"{value} {token}")
        sentence = ", ".join(items)
        if(afs['usd_value']):
            sentence += f" worth ${afs['usd_value']}"
        return sentence


class BountyFulfillmentQuerySet(models.QuerySet):
    """Handle the manager queryset for BountyFulfillments."""

    def accepted(self):
        """Filter results to accepted bounty fulfillments."""
        return self.filter(accepted=True)

    def submitted(self):
        """Exclude results that have not been submitted."""
        return self.exclude(fulfiller_address='0x0000000000000000000000000000000000000000')


class BountyFulfillment(SuperModel):
    """The structure of a fulfillment on a Bounty."""

    fulfiller_address = models.CharField(max_length=50)
    fulfiller_email = models.CharField(max_length=255, blank=True)
    fulfiller_github_username = models.CharField(max_length=255, blank=True)
    fulfiller_name = models.CharField(max_length=255, blank=True)
    fulfiller_metadata = JSONField(default={}, blank=True)
    fulfillment_id = models.IntegerField(null=True, blank=True)
    fulfiller_hours_worked = models.DecimalField(null=True, blank=True, decimal_places=2, max_digits=50)
    fulfiller_github_url = models.CharField(max_length=255, blank=True, null=True)
    accepted = models.BooleanField(default=False)
    accepted_on = models.DateTimeField(null=True, blank=True)

    bounty = models.ForeignKey(Bounty, related_name='fulfillments', on_delete=models.CASCADE)
    profile = models.ForeignKey('dashboard.Profile', related_name='fulfilled', on_delete=models.CASCADE, null=True)

    def __str__(self):
        """Define the string representation of BountyFulfillment.

        Returns:
            str: The string representation of the object.

        """
        return f'BountyFulfillment ID: ({self.pk}) - Bounty ID: ({self.bounty.pk})'

    def save(self, *args, **kwargs):
        """Define custom handling for saving bounty fulfillments."""
        if self.fulfiller_github_username:
            self.fulfiller_github_username = self.fulfiller_github_username.lstrip('@')
        super().save(*args, **kwargs)

    @property
    def to_json(self):
        """Define the JSON representation of BountyFulfillment.

        Returns:
            dict: A JSON representation of BountyFulfillment.

        """
        return {
            'address': self.fulfiller_address,
            'bounty_id': self.bounty.pk,
            'email': self.fulfiller_email,
            'githubUsername': self.fulfiller_github_username,
            'name': self.fulfiller_name,
        }


class BountySyncRequest(SuperModel):
    """Define the structure for bounty syncing."""

    github_url = models.URLField()
    processed = models.BooleanField()


class Subscription(SuperModel):

    email = models.EmailField(max_length=255)
    raw_data = models.TextField()
    ip = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.email} {self.created_on}"


class TipPayoutException(Exception):
    pass


class Tip(SuperModel):

    web3_type = models.CharField(max_length=50, default='v3')
    emails = JSONField()
    url = models.CharField(max_length=255, default='', blank=True)
    tokenName = models.CharField(max_length=255)
    tokenAddress = models.CharField(max_length=255)
    amount = models.DecimalField(default=1, decimal_places=4, max_digits=50)
    comments_priv = models.TextField(default='', blank=True)
    comments_public = models.TextField(default='', blank=True)
    ip = models.CharField(max_length=50)
    expires_date = models.DateTimeField()
    github_url = models.URLField(null=True, blank=True)
    from_name = models.CharField(max_length=255, default='', blank=True)
    from_email = models.CharField(max_length=255, default='', blank=True)
    from_username = models.CharField(max_length=255, default='', blank=True)
    username = models.CharField(max_length=255, default='')  # to username
    network = models.CharField(max_length=255, default='')
    txid = models.CharField(max_length=255, default='')
    receive_txid = models.CharField(max_length=255, default='', blank=True)
    received_on = models.DateTimeField(null=True, blank=True)
    from_address = models.CharField(max_length=255, default='', blank=True)
    receive_address = models.CharField(max_length=255, default='', blank=True)
    recipient_profile = models.ForeignKey(
        'dashboard.Profile', related_name='received_tips', on_delete=models.SET_NULL, null=True, blank=True
    )
    sender_profile = models.ForeignKey(
        'dashboard.Profile', related_name='sent_tips', on_delete=models.SET_NULL, null=True, blank=True
    )
    metadata = JSONField(default={}, blank=True)
    is_for_bounty_fulfiller = models.BooleanField(
        default=False,
        help_text='If this option is chosen, this tip will be automatically paid to the bounty'
                  ' fulfiller, not self.usernameusername.',
    )

    def __str__(self):
        """Return the string representation for a tip."""
        if self.web3_type == 'yge':
            return f"({self.network}) - {self.status}{' ORPHAN' if not self.emails else ''} " \
               f"{self.amount} {self.tokenName} to {self.username} from {self.from_name or 'NA'}, " \
               f"created: {naturalday(self.created_on)}, expires: {naturalday(self.expires_date)}"
        status = 'funded' if self.txid else 'not funded'
        status = status if not self.receive_txid else 'received'
        return f"({self.web3_type}) {status} {self.amount} {self.tokenName} to {self.username} from {self.from_name or 'NA'}"

    # TODO: DRY
    def get_natural_value(self):
        token = addr_to_token(self.tokenAddress)
        decimals = token['decimals']
        return float(self.amount) / 10**decimals

    @property
    def value_true(self):
        return self.get_natural_value()

    @property
    def amount_in_wei(self):
        token = addr_to_token(self.tokenAddress)
        decimals = token['decimals'] if token else 18
        return float(self.amount) * 10**decimals

    @property
    def amount_in_whole_units(self):
        return float(self.amount)

    @property
    def org_name(self):
        try:
            return org_name(self.url)
        except Exception:
            return None

    @property
    def receive_url(self):
        if self.web3_type == 'yge':
            return self.url
        elif self.web3_type == 'v3':
            return self.receive_url_for_recipient
        elif self.web3_type != 'v2':
            raise Exception

        pk = self.metadata.get('priv_key')
        txid = self.txid
        network = self.network
        return f"{settings.BASE_URL}tip/receive/v2/{pk}/{txid}/{network}"

    @property
    def receive_url_for_recipient(self):
        if self.web3_type != 'v3':
            raise Exception

        try:
            key = self.metadata['reference_hash_for_receipient']
            return f"{settings.BASE_URL}tip/receive/v3/{key}/{self.txid}/{self.network}"
        except:
            return None

    # TODO: DRY
    @property
    def value_in_eth(self):
        if self.tokenName == 'ETH':
            return self.amount
        try:
            return convert_amount(self.amount, self.tokenName, 'ETH')
        except Exception:
            return None

    @property
    def value_in_usdt_now(self):
        decimals = 1
        if self.tokenName in ['USDT', 'DAI']:
            return float(self.amount)
        try:
            return round(float(convert_amount(self.amount, self.tokenName, 'USDT')) / decimals, 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def value_in_usdt(self):
        return self.value_in_usdt_then

    @property
    def value_in_usdt_then(self):
        decimals = 1
        if self.tokenName in ['USDT', 'DAI']:
            return float(self.amount)
        try:
            return round(float(convert_amount(self.amount, self.tokenName, 'USDT', self.created_on)) / decimals, 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def token_value_in_usdt_now(self):
        try:
            return round(convert_token_to_usdt(self.tokenName), 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def token_value_in_usdt_then(self):
        try:
            return round(convert_token_to_usdt(self.tokenName, self.created_on), 2)
        except ConversionRateNotFoundError:
            return None

    @property
    def status(self):
        if self.receive_txid:
            return "RECEIVED"
        return "PENDING"

    @property
    def github_org_name(self):
        try:
            return org_name(self.github_url)
        except Exception:
            return None

    def is_notification_eligible(self, var_to_check=True):
        """Determine whether or not a notification is eligible for transmission outside of production.

        Returns:
            bool: Whether or not the Tip is eligible for outbound notifications.

        """
        if not var_to_check or self.network != settings.ENABLE_NOTIFICATIONS_ON_NETWORK:
            return False
        if self.network == 'mainnet' and (settings.DEBUG or settings.ENV != 'prod'):
            return False
        if (settings.DEBUG or settings.ENV != 'prod') and settings.GITHUB_API_USER != self.github_org_name:
            return False
        return True

    @property
    def bounty(self):
        try:
            return Bounty.objects.current().filter(
                github_url__iexact=self.github_url,
                network=self.network).order_by('-web3_created').first()
        except Bounty.DoesNotExist:
            return None

    def payout_to(self, address, amount_override=None):
        # TODO: deprecate this after v3 is shipped.
        from dashboard.utils import get_web3
        from dashboard.abi import erc20_abi
        if not address or address == '0x0':
            raise TipPayoutException('bad forwarding address')
        if self.web3_type == 'yge':
            raise TipPayoutException('bad web3_type')
        if self.receive_txid:
            raise TipPayoutException('already received')

        # send tokens
        tip = self
        address = Web3.toChecksumAddress(address)
        w3 = get_web3(tip.network)
        is_erc20 = tip.tokenName.lower() != 'eth'
        amount = int(tip.amount_in_wei) if not amount_override else int(amount_override)
        gasPrice = recommend_min_gas_price_to_confirm_in_time(60) * 10**9
        from_address = Web3.toChecksumAddress(tip.metadata['address'])
        nonce = w3.eth.getTransactionCount(from_address)
        if is_erc20:
            # ERC20 contract receive
            balance = w3.eth.getBalance(from_address)
            contract = w3.eth.contract(Web3.toChecksumAddress(tip.tokenAddress), abi=erc20_abi)
            gas = contract.functions.transfer(address, amount).estimateGas({'from': from_address}) + 1
            gasPrice = gasPrice if ((gas * gasPrice) < balance) else (balance * 1.0 / gas)
            tx = contract.functions.transfer(address, amount).buildTransaction({
                'nonce': nonce,
                'gas': w3.toHex(gas),
                'gasPrice': w3.toHex(int(gasPrice)),
            })
        else:
            # ERC20 contract receive
            gas = 100000
            amount -= gas * int(gasPrice)
            tx = dict(
                nonce=nonce,
                gasPrice=w3.toHex(int(gasPrice)),
                gas=w3.toHex(gas),
                to=address,
                value=w3.toHex(amount),
                data=b'',
            )
        signed = w3.eth.account.signTransaction(tx, tip.metadata['priv_key'])
        receive_txid = w3.eth.sendRawTransaction(signed.rawTransaction).hex()
        return receive_txid

@receiver(pre_save, sender=Tip, dispatch_uid="psave_tip")
def psave_tip(sender, instance, **kwargs):
    # when a new tip is saved, make sure it doesnt have whitespace in it
    instance.username = instance.username.replace(' ', '')


# @receiver(pre_save, sender=Bounty, dispatch_uid="normalize_usernames")
# def normalize_usernames(sender, instance, **kwargs):
#     if instance.bounty_owner_github_username:
#         instance.bounty_owner_github_username = instance.bounty_owner_github_username.lstrip('@')


# method for updating
@receiver(pre_save, sender=Bounty, dispatch_uid="psave_bounty")
def psave_bounty(sender, instance, **kwargs):
    idx_experience_level = {
        'Unknown': 1,
        'Beginner': 2,
        'Intermediate': 3,
        'Advanced': 4,
    }

    idx_project_length = {
        'Unknown': 1,
        'Hours': 2,
        'Days': 3,
        'Weeks': 4,
        'Months': 5,
    }

    instance.idx_status = instance.status
    instance.fulfillment_accepted_on = instance.get_fulfillment_accepted_on
    instance.fulfillment_submitted_on = instance.get_fulfillment_submitted_on
    instance.fulfillment_started_on = instance.get_fulfillment_started_on
    instance._val_usd_db = instance.get_value_in_usdt if instance.get_value_in_usdt else 0
    instance._val_usd_db_now = instance.get_value_in_usdt_now if instance.get_value_in_usdt_now else 0
    instance.idx_experience_level = idx_experience_level.get(instance.experience_level, 0)
    instance.idx_project_length = idx_project_length.get(instance.project_length, 0)
    instance.token_value_time_peg = instance.get_token_value_time_peg
    instance.token_value_in_usdt = instance.get_token_value_in_usdt
    instance.value_in_usdt_now = instance.get_value_in_usdt_now
    instance.value_in_usdt = instance.get_value_in_usdt
    instance.value_in_eth = instance.get_value_in_eth
    instance.value_true = instance.get_value_true


class InterestQuerySet(models.QuerySet):
    """Handle the manager queryset for Interests."""

    def needs_review(self):
        """Filter results to Interest objects requiring review by moderators."""
        return self.filter(status=Interest.STATUS_REVIEW)

    def warned(self):
        """Filter results to Interest objects that are currently in warning."""
        return self.filter(status=Interest.STATUS_WARNED)


class Interest(models.Model):
    """Define relationship for profiles expressing interest on a bounty."""

    STATUS_REVIEW = 'review'
    STATUS_WARNED = 'warned'
    STATUS_OKAY = 'okay'
    STATUS_SNOOZED = 'snoozed'
    STATUS_PENDING = 'pending'

    WORK_STATUSES = (
        (STATUS_REVIEW, 'Needs Review'),
        (STATUS_WARNED, 'Hunter Warned'),
        (STATUS_OKAY, 'Okay'),
        (STATUS_SNOOZED, 'Snoozed'),
        (STATUS_PENDING, 'Pending'),
    )

    profile = models.ForeignKey('dashboard.Profile', related_name='interested', on_delete=models.CASCADE)
    created = models.DateTimeField(auto_now_add=True, blank=True, null=True, verbose_name=_('Date Created'))
    issue_message = models.TextField(default='', blank=True, verbose_name=_('Issue Comment'))
    pending = models.BooleanField(
        default=False,
        help_text=_('If this option is chosen, this interest is pending and not yet active'),
        verbose_name=_('Pending'),
    )
    acceptance_date = models.DateTimeField(blank=True, null=True, verbose_name=_('Date Accepted'))
    status = models.CharField(
        choices=WORK_STATUSES,
        default=STATUS_OKAY,
        max_length=7,
        help_text=_('Whether or not the interest requires review'),
        verbose_name=_('Needs Review'))

    # Interest QuerySet Manager
    objects = InterestQuerySet.as_manager()

    def __str__(self):
        """Define the string representation of an interested profile."""
        return f"{self.profile.handle} / pending: {self.pending} / status: {self.status}"

    @property
    def bounties(self):
        return Bounty.objects.filter(interested=self)

    def change_status(self, status=None):
        if status is None or status not in self.WORK_STATUSES:
            return self
        self.status = status
        self.save()
        return self

    def mark_for_review(self):
        """Flag the Interest for review by the moderation team."""
        self.status = self.STATUS_REVIEW
        self.save()
        return self


@receiver(post_save, sender=Interest, dispatch_uid="psave_interest")
@receiver(post_delete, sender=Interest, dispatch_uid="pdel_interest")
def psave_interest(sender, instance, **kwargs):
    # when a new interest is saved, update the status on frontend
    print("signal: updating bounties psave_interest")
    for bounty in Bounty.objects.filter(interested=instance):
        bounty.save()


class ActivityQuerySet(models.QuerySet):
    """Handle the manager queryset for Activities."""

    def needs_review(self):
        """Filter results to Activity objects to be reviewed by moderators."""
        return self.select_related('bounty', 'profile').filter(needs_review=True)

    def reviewed(self):
        """Filter results to Activity objects to be reviewed by moderators."""
        return self.select_related('bounty', 'profile').filter(
            needs_review=False,
            activity_type__in=['bounty_abandonment_escalation_to_mods', 'bounty_abandonment_warning'],
        )

    def warned(self):
        """Filter results to Activity objects to be reviewed by moderators."""
        return self.select_related('bounty', 'profile').filter(
            activity_type='bounty_abandonment_warning',
        )

    def escalated_for_removal(self):
        """Filter results to Activity objects to be reviewed by moderators."""
        return self.select_related('bounty', 'profile').filter(
            activity_type='bounty_abandonment_escalation_to_mods',
        )


class Activity(models.Model):
    """Represent Start work/Stop work event.

    Attributes:
        ACTIVITY_TYPES (list of tuples): The valid activity types.

    """

    ACTIVITY_TYPES = [
        ('new_bounty', 'New Bounty'),
        ('start_work', 'Work Started'),
        ('stop_work', 'Work Stopped'),
        ('work_submitted', 'Work Submitted'),
        ('work_done', 'Work Done'),
        ('worker_approved', 'Worker Approved'),
        ('worker_rejected', 'Worker Rejected'),
        ('worker_applied', 'Worker Applied'),
        ('increased_bounty', 'Increased Funding'),
        ('killed_bounty', 'Canceled Bounty'),
        ('new_tip', 'New Tip'),
        ('receive_tip', 'Tip Received'),
        ('bounty_abandonment_escalation_to_mods', 'Escalated for Abandonment of Bounty'),
        ('bounty_abandonment_warning', 'Warning for Abandonment of Bounty'),
        ('bounty_removed_slashed_by_staff', 'Dinged and Removed from Bounty by Staff'),
        ('bounty_removed_by_staff', 'Removed from Bounty by Staff'),
        ('bounty_removed_by_funder', 'Removed from Bounty by Funder'),
        ('new_crowdfund', 'New Crowdfund Contribution'),
    ]

    profile = models.ForeignKey('dashboard.Profile', related_name='activities', on_delete=models.CASCADE)
    bounty = models.ForeignKey('dashboard.Bounty', related_name='activities', on_delete=models.CASCADE, blank=True, null=True)
    tip = models.ForeignKey('dashboard.Tip', related_name='activities', on_delete=models.CASCADE, blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    activity_type = models.CharField(max_length=50, choices=ACTIVITY_TYPES, blank=True)
    metadata = JSONField(default={})
    needs_review = models.BooleanField(default=False)

    # Activity QuerySet Manager
    objects = ActivityQuerySet.as_manager()

    def __str__(self):
        """Define the string representation of an interested profile."""
        return f"{self.profile.handle} type: {self.activity_type} created: {naturalday(self.created)} " \
               f"needs review: {self.needs_review}"

    def i18n_name(self):
        return _(next((x[1] for x in self.ACTIVITY_TYPES if x[0] == self.activity_type), 'Unknown type'))

    @property
    def view_props(self):
        from dashboard.tokens import token_by_name
        icons = {
            'new_tip': 'fa-thumbs-up',
            'start_work': 'fa-lightbulb',
            'new_bounty': 'fa-money-bill-alt',
            'work_done': 'fa-check-circle',
        }

        activity = self
        activity.icon = icons.get(activity.activity_type, 'fa-check-circle')
        obj = activity.metadata
        if 'new_bounty' in activity.metadata:
            obj = activity.metadata['new_bounty']
        activity.title = obj.get('title', '')
        if 'id' in obj:
            activity.bounty_url = Bounty.objects.get(pk=obj['id']).get_relative_url()
            if activity.title:
                activity.urled_title = f'<a href="{activity.bounty_url}">{activity.title}</a>'
            else:
                activity.urled_title = activity.title
        if 'value_in_usdt_now' in obj:
            activity.value_in_usdt_now = obj['value_in_usdt_now']
        if 'token_name' in obj:
            activity.token = token_by_name(obj['token_name'])
            if 'value_in_token' in obj and activity.token:
                activity.value_in_token_disp = round((float(obj['value_in_token']) /
                                                      10 ** activity.token['decimals']) * 1000) / 1000
        return activity

    @property
    def token_name(self):
        if self.bounty:
            return self.bounty.token_name
        if 'token_name' in self.metadata.keys():
            return self.metadata['token_name']
        return None


class Profile(SuperModel):
    """Define the structure of the user profile.

    TODO:
        * Remove all duplicate identity related information already stored on User.

    """

    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True)
    data = JSONField()
    handle = models.CharField(max_length=255, db_index=True)
    avatar = models.ForeignKey('avatar.Avatar', on_delete=models.SET_NULL, null=True, blank=True)
    last_sync_date = models.DateTimeField(null=True)
    email = models.CharField(max_length=255, blank=True, db_index=True)
    github_access_token = models.CharField(max_length=255, blank=True, db_index=True)
    pref_lang_code = models.CharField(max_length=2, choices=settings.LANGUAGES, blank=True)
    slack_repos = ArrayField(models.CharField(max_length=200), blank=True, default=[])
    slack_token = models.CharField(max_length=255, default='', blank=True)
    slack_channel = models.CharField(max_length=255, default='', blank=True)
    discord_repos = ArrayField(models.CharField(max_length=200), blank=True, default=[])
    discord_webhook_url = models.CharField(max_length=400, default='', blank=True)
    suppress_leaderboard = models.BooleanField(
        default=False,
        help_text='If this option is chosen, we will remove your profile information from the leaderboard',
    )
    hide_profile = models.BooleanField(
        default=True,
        help_text='If this option is chosen, we will remove your profile information all_together',
    )
    trust_profile = models.BooleanField(
        default=False,
        help_text='If this option is chosen, the user is able to submit a faucet/ens domain registration even if they are new to github',
    )
    form_submission_records = JSONField(default=[], blank=True)
    # Sample data: https://gist.github.com/mbeacom/ee91c8b0d7083fa40d9fa065125a8d48
    max_num_issues_start_work = models.IntegerField(default=3)
    preferred_payout_address = models.CharField(max_length=255, default='', blank=True)
    max_tip_amount_usdt_per_tx = models.DecimalField(default=500, decimal_places=2, max_digits=50)
    max_tip_amount_usdt_per_week = models.DecimalField(default=1500, decimal_places=2, max_digits=50)

    @property
    def is_org(self):
        try:
            return self.data['type'] == 'Organization'
        except:
            return False

    @property
    def bounties(self):
        fulfilled_bounty_ids = self.fulfilled.all().values_list('bounty_id')
        bounties = Bounty.objects.filter(github_url__istartswith=self.github_url, current_bounty=True)
        for interested in self.interested.all():
            bounties = bounties | Bounty.objects.filter(interested=interested, current_bounty=True)
        bounties = bounties | Bounty.objects.filter(pk__in=fulfilled_bounty_ids, current_bounty=True)
        bounties = bounties | Bounty.objects.filter(bounty_owner_github_username__iexact=self.handle, current_bounty=True) | Bounty.objects.filter(bounty_owner_github_username__iexact="@" + self.handle, current_bounty=True)
        bounties = bounties | Bounty.objects.filter(github_url__in=[url for url in self.tips.values_list('github_url', flat=True)], current_bounty=True)
        bounties = bounties.distinct()
        return bounties.order_by('-web3_created')

    @property
    def tips(self):
        on_repo = Tip.objects.filter(github_url__startswith=self.github_url).order_by('-id')
        tipped_for = Tip.objects.filter(username__iexact=self.handle).order_by('-id')
        return on_repo | tipped_for

    def no_times_slashed_by_staff(self):
        user_actions = UserAction.objects.filter(
            profile=self,
            action='bounty_removed_slashed_by_staff',
            )
        return user_actions.count()

    def no_times_been_removed_by_funder(self):
        user_actions = UserAction.objects.filter(
            profile=self,
            action='bounty_removed_by_funder',
            )
        return user_actions.count()

    def no_times_been_removed_by_staff(self):
        user_actions = UserAction.objects.filter(
            profile=self,
            action='bounty_removed_by_staff',
            )
        return user_actions.count()

    @property
    def desc(self):
        stats = self.stats
        role = stats[0][0]
        total_funded_participated = stats[1][0]
        plural = 's' if total_funded_participated != 1 else ''
        return f"@{self.handle} is a {role} who has participated in {total_funded_participated} " \
               f"funded issue{plural} on Gitcoin"

    @property
    def github_created_on(self):
        from datetime import datetime
        created_on = datetime.strptime(self.data['created_at'], '%Y-%m-%dT%H:%M:%SZ')
        return created_on.replace(tzinfo=pytz.UTC)

    @property
    def repos_data(self):
        from git.utils import get_user
        from app.utils import add_contributors
        # TODO: maybe rewrite this so it doesnt have to go to the internet to get the info
        # but in a way that is respectful of db size too
        repos_data = get_user(self.handle, '/repos')
        repos_data = sorted(repos_data, key=lambda repo: repo['stargazers_count'], reverse=True)
        repos_data = [add_contributors(repo_data) for repo_data in repos_data]
        return repos_data

    @property
    def is_moderator(self):
        """Determine whether or not the user is a moderator.

        Returns:
            bool: Whether or not the user is a moderator.

        """
        return self.user.groups.filter(name='Moderators').exists() if self.user else False

    @property
    def is_staff(self):
        """Determine whether or not the user is a staff member.

        Returns:
            bool: Whether or not the user is a member of the staff.

        """
        return self.user.is_staff if self.user else False

    @property
    def stats(self):
        bounties = self.bounties.stats_eligible()
        loyalty_rate = 0
        total_funded = sum([
            bounty.value_in_usdt if bounty.value_in_usdt else 0
            for bounty in bounties if bounty.is_funder(self.handle)
        ])
        total_fulfilled = sum([
            bounty.value_in_usdt if bounty.value_in_usdt else 0
            for bounty in bounties if bounty.is_hunter(self.handle)
        ])
        print(total_funded, total_fulfilled)
        role = 'newbie'
        if total_funded > total_fulfilled:
            role = 'funder'
        elif total_funded < total_fulfilled:
            role = 'coder'

        loyalty_rate = self.fulfilled.filter(accepted=True).count()
        success_rate = 0
        if bounties.exists():
            numer = bounties.filter(idx_status__in=['submitted', 'started', 'done']).count()
            denom = bounties.exclude(idx_status__in=['open']).count()
            success_rate = int(round(numer * 1.0 / denom, 2) * 100) if denom != 0 else 'N/A'
        if success_rate == 0:
            success_rate = 'N/A'
            loyalty_rate = 'N/A'
        else:
            success_rate = f"{success_rate}%"
            loyalty_rate = f"{loyalty_rate}x"
        if role == 'newbie':
            return [
                (role, 'Status'),
                (bounties.count(), 'Total Funded Issues'),
                (bounties.filter(idx_status='open').count(), 'Open Funded Issues'),
                (loyalty_rate, 'Loyalty Rate'),
                (total_fulfilled, 'Bounties completed'),
            ]
        elif role == 'coder':
            return [
                (role, 'Primary Role'),
                (bounties.count(), 'Total Funded Issues'),
                (success_rate, 'Success Rate'),
                (loyalty_rate, 'Loyalty Rate'),
                (total_fulfilled, 'Bounties completed'),
            ]
        # funder
        return [
            (role, 'Primary Role'),
            (bounties.count(), 'Total Funded Issues'),
            (bounties.filter(idx_status='open').count(), 'Open Funded Issues'),
            (success_rate, 'Success Rate'),
            (total_fulfilled, 'Bounties completed'),
        ]

    @property
    def get_quarterly_stats(self):
        """Generate last 90 days stats for this user.

        Returns:
            dict : containing the following information
            'user_total_earned_eth': Total earnings of user in ETH.
            'user_total_earned_usd': Total earnings of user in USD.
            'user_total_funded_usd': Total value of bounties funded by the user on bounties in done status in USD
            'user_total_funded_hours': Total hours input by the developers on the fulfillment of bounties created by the user in USD
            'user_fulfilled_bounties_count': Total bounties fulfilled by user
            'user_fufilled_bounties': bool, if the user fulfilled bounties
            'user_funded_bounties_count': Total bounties funded by the user
            'user_funded_bounties': bool, if the user funded bounties in the last quarter
            'user_funded_bounty_developers': Unique set of users that fulfilled bounties funded by the user
            'user_avg_hours_per_funded_bounty': Average hours input by developer on fulfillment per bounty
            'user_avg_hourly_rate_per_funded_bounty': Average hourly rate in dollars per bounty funded by user
            'user_avg_eth_earned_per_bounty': Average earning in ETH earned by user per bounty
            'user_avg_usd_earned_per_bounty': Average earning in USD earned by user per bounty
            'user_num_completed_bounties': Total no. of bounties completed.
            'user_num_funded_fulfilled_bounties': Total bounites that were funded by the user and fulfilled
            'user_bounty_completion_percentage': Percentage of bounties successfully completed by the user
            'user_funded_fulfilled_percentage': Percentage of bounties funded by the user that were fulfilled
            'user_active_in_last_quarter': bool, if the user was active in last quarter
            'user_no_of_languages': No of languages user used while working on bounties.
            'user_languages': Languages that were used in bounties that were worked on.
            'relevant_bounties': a list of Bounty(s) that would match the skillset input by the user into the Match tab of their settings
        """
        user_active_in_last_quarter = False
        user_fulfilled_bounties = False
        user_funded_bounties = False
        last_quarter = datetime.now() - timedelta(days=90)
        bounties = self.bounties.filter(modified_on__gte=last_quarter)
        fulfilled_bounties = [
            bounty for bounty in bounties if bounty.is_hunter(self.handle) and bounty.status == 'done'
        ]
        fulfilled_bounties_count = len(fulfilled_bounties)
        funded_bounties = self.get_funded_bounties()
        funded_bounties_count = funded_bounties.count()
        from django.db.models import Sum
        if funded_bounties_count:
            total_funded_usd = funded_bounties.all().aggregate(Sum('value_in_usdt'))['value_in_usdt__sum']
            total_funded_hourly_rate = float(0)
            hourly_rate_bounties_counted = float(0)
            for bounty in funded_bounties:
                hourly_rate = bounty.hourly_rate
                if hourly_rate:
                    total_funded_hourly_rate += bounty.hourly_rate
                    hourly_rate_bounties_counted += 1
            funded_bounty_fulfillments = []
            for bounty in funded_bounties:
                fulfillments = bounty.fulfillments.filter(accepted=True)
                for fulfillment in fulfillments:
                    if isinstance(fulfillment, BountyFulfillment):
                        funded_bounty_fulfillments.append(fulfillment)
            funded_bounty_fulfillments_count = len(funded_bounty_fulfillments)

            total_funded_hours = 0
            funded_fulfillments_with_hours_counted = 0
            if funded_bounty_fulfillments_count:
                from decimal import Decimal
                for fulfillment in funded_bounty_fulfillments:
                    if isinstance(fulfillment.fulfiller_hours_worked, Decimal):
                        total_funded_hours += fulfillment.fulfiller_hours_worked
                        funded_fulfillments_with_hours_counted += 1

            user_funded_bounty_developers = []
            for fulfillment in funded_bounty_fulfillments:
                user_funded_bounty_developers.append(fulfillment.fulfiller_github_username.lstrip('@'))
            user_funded_bounty_developers = [*{*user_funded_bounty_developers}]
            if funded_fulfillments_with_hours_counted:
                avg_hourly_rate_per_funded_bounty = \
                    float(total_funded_hourly_rate) / float(funded_fulfillments_with_hours_counted)
                avg_hours_per_funded_bounty = \
                    float(total_funded_hours) / float(funded_fulfillments_with_hours_counted)
            else:
                avg_hourly_rate_per_funded_bounty = 0
                avg_hours_per_funded_bounty = 0
            funded_fulfilled_bounties = [
                bounty for bounty in funded_bounties if bounty.status == 'done'
            ]
            num_funded_fulfilled_bounties = len(funded_fulfilled_bounties)
            funded_fulfilled_percent = float(
                # Round to 0 places of decimals to be displayed in template
                round(num_funded_fulfilled_bounties * 1.0 / funded_bounties_count, 2) * 100
            )
            user_funded_bounties = True
        else:
            num_funded_fulfilled_bounties = 0
            funded_fulfilled_percent = 0
            user_funded_bounties = False
            avg_hourly_rate_per_funded_bounty = 0
            avg_hours_per_funded_bounty = 0
            total_funded_usd = 0
            total_funded_hours = 0
            user_funded_bounty_developers = []

        total_earned_eth = sum([
            bounty.value_in_eth if bounty.value_in_eth else 0
            for bounty in fulfilled_bounties
        ])
        total_earned_eth /= 10**18
        total_earned_usd = sum([
            bounty.value_in_usdt if bounty.value_in_usdt else 0
            for bounty in fulfilled_bounties
        ])

        num_completed_bounties = bounties.filter(idx_status__in=['done']).count()
        terminal_state_bounties = bounties.filter(idx_status__in=Bounty.TERMINAL_STATUSES).count()
        completetion_percent = int(
            round(num_completed_bounties * 1.0 / terminal_state_bounties, 2) * 100
        ) if terminal_state_bounties != 0 else 0

        avg_eth_earned_per_bounty = 0
        avg_usd_earned_per_bounty = 0

        if fulfilled_bounties_count:
            avg_eth_earned_per_bounty = total_earned_eth / fulfilled_bounties_count
            avg_usd_earned_per_bounty = total_earned_usd / fulfilled_bounties_count
            user_fulfilled_bounties = True

        user_languages = []
        for bounty in fulfilled_bounties:
            user_languages += bounty.keywords.split(',')
        user_languages = set(user_languages)
        user_no_of_languages = len(user_languages)

        if num_completed_bounties or fulfilled_bounties_count:
            user_active_in_last_quarter = True
            relevant_bounties = []
        else:
            from marketing.utils import get_or_save_email_subscriber
            user_coding_languages = get_or_save_email_subscriber(self.email, 'internal').keywords
            if user_coding_languages is not None:
                potential_bounties = Bounty.objects.all()
                relevant_bounties = Bounty.objects.none()
                for keyword in user_coding_languages:
                    relevant_bounties = relevant_bounties.union(potential_bounties.filter(
                            network=Profile.get_network(),
                            current_bounty=True,
                            metadata__icontains=keyword,
                            idx_status__in=['open'],
                            ).order_by('?')
                    )
                relevant_bounties = relevant_bounties[:3]
                relevant_bounties = list(relevant_bounties)
        # Round to 2 places of decimals to be diplayed in templates
        completetion_percent = float('%.2f' % completetion_percent)
        funded_fulfilled_percent = float('%.2f' % funded_fulfilled_percent)
        avg_eth_earned_per_bounty = float('%.2f' % avg_eth_earned_per_bounty)
        avg_usd_earned_per_bounty = float('%.2f' % avg_usd_earned_per_bounty)
        avg_hourly_rate_per_funded_bounty = float('%.2f' % avg_hourly_rate_per_funded_bounty)
        avg_hours_per_funded_bounty = float('%.2f' % avg_hours_per_funded_bounty)
        total_earned_eth = float('%.2f' % total_earned_eth)
        total_earned_usd = float('%.2f' % total_earned_usd)

        user_languages = []
        for bounty in fulfilled_bounties:
            user_languages += bounty.keywords.split(',')
        user_languages = set(user_languages)
        user_no_of_languages = len(user_languages)

        return {
            'user_total_earned_eth': total_earned_eth,
            'user_total_earned_usd': total_earned_usd,
            'user_total_funded_usd': total_funded_usd,
            'user_total_funded_hours': total_funded_hours,
            'user_fulfilled_bounties_count': fulfilled_bounties_count,
            'user_fulfilled_bounties': user_fulfilled_bounties,
            'user_funded_bounties_count': funded_bounties_count,
            'user_funded_bounties': user_funded_bounties,
            'user_funded_bounty_developers': user_funded_bounty_developers,
            'user_avg_hours_per_funded_bounty': avg_hours_per_funded_bounty,
            'user_avg_hourly_rate_per_funded_bounty': avg_hourly_rate_per_funded_bounty,
            'user_avg_eth_earned_per_bounty': avg_eth_earned_per_bounty,
            'user_avg_usd_earned_per_bounty': avg_usd_earned_per_bounty,
            'user_num_completed_bounties': num_completed_bounties,
            'user_num_funded_fulfilled_bounties': num_funded_fulfilled_bounties,
            'user_bounty_completion_percentage': completetion_percent,
            'user_funded_fulfilled_percentage': funded_fulfilled_percent,
            'user_active_in_last_quarter': user_active_in_last_quarter,
            'user_no_of_languages': user_no_of_languages,
            'user_languages': user_languages,
            'relevant_bounties': relevant_bounties
        }

    @property
    def github_url(self):
        return f"https://github.com/{self.handle}"

    @property
    def avatar_url(self):
        return f"{settings.BASE_URL}static/avatar/{self.handle}"

    @property
    def avatar_url_with_gitcoin_logo(self):
        return f"{self.avatar_url}/1"

    @property
    def absolute_url(self):
        return self.get_absolute_url()

    @property
    def username(self):
        handle = ''
        if getattr(self, 'user', None) and self.user.username:
            handle = self.user.username
        # TODO: (mbeacom) Remove this check once we get rid of all the lingering identity shenanigans.
        elif self.handle:
            handle = self.handle
        return handle


    def is_github_token_valid(self):
        """Check whether or not a Github OAuth token is valid.

        Args:
            access_token (str): The Github OAuth token.

        Returns:
            bool: Whether or not the provided OAuth token is valid.

        """
        if not self.github_access_token:
            return False

        _params = build_auth_dict(self.github_access_token)
        url = TOKEN_URL.format(**_params)
        response = requests.get(
            url,
            auth=(_params['client_id'], _params['client_secret']),
            headers=HEADERS)

        if response.status_code == 200:
            return True
        return False

    def __str__(self):
        return self.handle

    def get_relative_url(self, preceding_slash=True):
        return f"{'/' if preceding_slash else ''}profile/{self.handle}"

    def get_absolute_url(self):
        return settings.BASE_URL + self.get_relative_url(preceding_slash=False)

    @property
    def url(self):
        return self.get_absolute_url()

    def get_access_token(self, save=True):
        """Get the Github access token from User.

        Args:
            save (bool): Whether or not to save the User access token to the profile.

        Raises:
            Exception: The exception is raised in the event of any error and returns an empty string.

        Returns:
            str: The Github access token.

        """
        try:
            access_token = self.user.social_auth.filter(provider='github').latest('pk').access_token
            if save:
                self.github_access_token = access_token
                self.save()
        except Exception:
            return ''
        return access_token

    def get_profile_preferred_language(self):
        return settings.LANGUAGE_CODE if not self.pref_lang_code else self.pref_lang_code

    def get_slack_repos(self, join=False):
        """Get the profile's slack tracked repositories.

        Args:
            join (bool): Whether or not to return a joined string representation.
                Defaults to: False.

        Returns:
            list of str: If joined is False, a list of slack repositories.
            str: If joined is True, a combined string of slack repositories.

        """
        if join:
            repos = ', '.join(self.slack_repos)
            return repos
        return self.slack_repos

    def update_slack_integration(self, token, channel, repos):
        """Update the profile's slack integration settings.

        Args:
            token (str): The profile's slack token.
            channel (str): The profile's slack channel.
            repos (list of str): The profile's github repositories to track.

        """
        repos = repos.split(',')
        self.slack_token = token
        self.slack_repos = [repo.strip() for repo in repos]
        self.slack_channel = channel
        self.save()

    def get_discord_repos(self, join=False):
        """Get the profile's Discord tracked repositories.

        Args:
            join (bool): Whether or not to return a joined string representation.
                Defaults to: False.

        Returns:
            list of str: If joined is False, a list of discord repositories.
            str: If joined is True, a combined string of discord repositories.

        """
        if join:
            repos = ', '.join(self.discord_repos)
            return repos
        return self.discord_repos

    def update_discord_integration(self, webhook_url, repos):
        """Update the profile's Discord integration settings.

        Args:
            webhook_url (str): The profile's Discord webhook url.
            repos (list of str): The profile's github repositories to track.

        """
        repos = repos.split(',')
        self.discord_webhook_url = webhook_url
        self.discord_repos = [repo.strip() for repo in repos]
        self.save()

    @staticmethod
    def get_network():
        return 'mainnet' if not settings.DEBUG else 'rinkeby'

    def get_fulfilled_bounties(self, network=None):
        network = network or self.get_network()
        fulfilled_bounty_ids = self.fulfilled.all().values_list('bounty_id', flat=True)
        bounties = Bounty.objects.filter(pk__in=fulfilled_bounty_ids, accepted=True, current_bounty=True, network=network)
        return bounties

    def get_orgs_bounties(self, network=None):
        network = network or self.get_network()
        url = f"https://github.com/{self.handle}"
        bounties = Bounty.objects.filter(current_bounty=True, network=network, github_url__contains=url)
        return bounties

    def get_leaderboard_index(self, key='quarterly_earners'):
        try:
            rank = LeaderboardRank.objects.filter(
                leaderboard=key,
                active=True,
                github_username=self.handle,
            ).latest('id')
            return rank.rank
        except LeaderboardRank.DoesNotExist:
            score = 0
        return score

    def get_contributor_leaderboard_index(self):
        return self.get_leaderboard_index()

    def get_funder_leaderboard_index(self):
        return self.get_leaderboard_index('quarterly_payers')

    def get_org_leaderboard_index(self):
        return self.get_leaderboard_index('quarterly_orgs')

    def get_eth_sum(self, sum_type='collected', network='mainnet'):
        """Get the sum of collected or funded ETH based on the provided type.

        Args:
            sum_type (str): The sum to lookup.  Defaults to: collected.
            network (str): The network to query results for.
                Defaults to: mainnet.

        Returns:
            float: The total sum of all ETH of the provided type.

        """
        eth_sum = 0

        if sum_type == 'funded':
            obj = self.get_funded_bounties(network=network)
        elif sum_type == 'collected':
            obj = self.get_fulfilled_bounties(network=network)
        elif sum_type == 'org':
            obj = self.get_orgs_bounties(network=network)

        try:
            if obj.exists():
                eth_sum = obj.aggregate(
                    Sum('value_in_eth')
                )['value_in_eth__sum'] / 10**18
        except Exception:
            pass

        return eth_sum

    def get_who_works_with(self, work_type='collected', network='mainnet'):
        """Get an array of profiles that this user works with.

        Args:
            work_type (str): The work type to lookup.  Defaults to: collected.
            network (str): The network to query results for.
                Defaults to: mainnet.

        Returns:
            dict: list of the profiles that were worked with (key) and the number of times they occured

        """
        if work_type == 'funded':
            obj = self.bounties_funded.filter(network=network)
        elif work_type == 'collected':
            obj = self.get_fulfilled_bounties(network=network)
        elif work_type == 'org':
            obj = self.get_orgs_bounties(network=network)

        if work_type != 'org':
            profiles = [bounty.org_name for bounty in obj if bounty.org_name]
        else:
            profiles = []
            for bounty in obj:
                for bf in bounty.fulfillments.filter(accepted=True):
                    if bf.fulfiller_github_username:
                        profiles.append(bf.fulfiller_github_username)

        profiles_dict = {profile: 0 for profile in profiles}
        for profile in profiles:
            profiles_dict[profile] += 1

        ordered_profiles_dict = collections.OrderedDict()
        for ele in sorted(profiles_dict.items(), key=lambda x: x[1], reverse=True):
            ordered_profiles_dict[ele[0]] = ele[1]
        return ordered_profiles_dict


    def get_funded_bounties(self, network='mainnet'):
        """Get the bounties that this user has funded

        Args:
            network (string): the network to look at.
                Defaults to: mainnet.


        Returns:
            queryset: list of bounties

        """

        funded_bounties = Bounty.objects.current().filter(
            Q(bounty_owner_github_username__iexact=self.handle) |
            Q(bounty_owner_github_username__iexact=f'@{self.handle}')
        )
        funded_bounties = funded_bounties.filter(network=network)
        return funded_bounties


    def to_dict(self, activities=True, leaderboards=True, network=None, tips=True):
        """Get the dictionary representation with additional data.

        Args:
            activities (bool): Whether or not to include activity queryset data.
                Defaults to: True.
            leaderboards (bool): Whether or not to include leaderboard position data.
                Defaults to: True.
            network (str): The Ethereum network to use for relevant queries.
                Defaults to: None (Environment specific).
            tips (bool): Whether or not to include tip data.
                Defaults to: True.

        Attributes:
            params (dict): The context dictionary to be returned.
            query_kwargs (dict): The kwargs to be passed to all queries
                throughout the method.
            sum_eth_funded (float): The total amount of ETH funded.
            sum_eth_collected (float): The total amount of ETH collected.

        Returns:
            dict: The profile card context.

        """
        params = {}
        network = network or self.get_network()

        query_kwargs = {'network': network}

        sum_eth_funded = self.get_eth_sum(sum_type='funded', **query_kwargs)
        sum_eth_collected = self.get_eth_sum(**query_kwargs)
        works_with_funded = self.get_who_works_with(work_type='funded', **query_kwargs)
        works_with_collected = self.get_who_works_with(work_type='collected', **query_kwargs)
        funded_bounties = self.get_funded_bounties(network=network)

        # org only
        works_with_org = []
        count_bounties_on_repo = 0
        sum_eth_on_repos = 0
        if self.is_org:
            works_with_org = self.get_who_works_with(work_type='org', **query_kwargs)
            count_bounties_on_repo = self.get_orgs_bounties(network=network).count()
            sum_eth_on_repos = self.get_eth_sum(sum_type='org', **query_kwargs)

        no_times_been_removed = self.no_times_been_removed_by_funder() + self.no_times_been_removed_by_staff() + self.no_times_slashed_by_staff()
        params = {
            'title': f"@{self.handle}",
            'active': 'profile_details',
            'newsletter_headline': _('Be the first to know about new funded issues.'),
            'card_title': f'@{self.handle} | Gitcoin',
            'card_desc': self.desc,
            'avatar_url': self.avatar_url_with_gitcoin_logo,
            'profile': self,
            'bounties': self.bounties,
            'count_bounties_completed': self.fulfilled.filter(accepted=True, bounty__network=network).count(),
            'sum_eth_collected': sum_eth_collected,
            'sum_eth_funded': sum_eth_funded,
            'works_with_collected': works_with_collected,
            'works_with_funded': works_with_funded,
            'funded_bounties_count': funded_bounties.count(),
            'activities': [{'title': _('No data available.')}],
            'no_times_been_removed': no_times_been_removed,
            'sum_eth_on_repos': sum_eth_on_repos,
            'works_with_org': works_with_org,
            'count_bounties_on_repo': count_bounties_on_repo,
        }

        if activities:
            fulfilled = self.fulfilled.filter(
                bounty__network=network
            ).select_related('bounty').all().order_by('-created_on')
            completed = list(set([fulfillment.bounty for fulfillment in fulfilled.exclude(accepted=False)]))
            submitted = list(set([fulfillment.bounty for fulfillment in fulfilled.exclude(accepted=True)]))
            started = self.interested.prefetch_related('bounty_set') \
                .filter(bounty__network=network).all().order_by('-created')
            started_bounties = list(set([interest.bounty_set.last() for interest in started]))

            if completed or submitted or started:
                params['activities'] = [{
                    'title': _('By Created Date'),
                    'completed': completed,
                    'submitted': submitted,
                    'started': started_bounties,
                }]

        if tips:
            params['tips'] = self.tips.filter(**query_kwargs)

        if leaderboards:
            params['scoreboard_position_contributor'] = self.get_contributor_leaderboard_index()
            params['scoreboard_position_funder'] = self.get_funder_leaderboard_index()
            if self.is_org:
                params['scoreboard_position_org'] = self.get_org_leaderboard_index()

        return params

    @property
    def is_eu(self):
        from app.utils import get_country_from_ip
        try:
            ip_addresses = list(set(self.actions.filter(action='Login').values_list('ip_address', flat=True)))
            for ip_address in ip_addresses:
                country = get_country_from_ip(ip_address)
                if country.continent.code == 'EU':
                    return True
        except Exception:
            pass
        return False


@receiver(user_logged_in)
def post_login(sender, request, user, **kwargs):
    """Handle actions to take on user login."""
    from dashboard.utils import create_user_action
    create_user_action(user, 'Login', request)


@receiver(user_logged_out)
def post_logout(sender, request, user, **kwargs):
    """Handle actions to take on user logout."""
    from dashboard.utils import create_user_action
    create_user_action(user, 'Logout', request)


class ProfileSerializer(serializers.BaseSerializer):
    """Handle serializing the Profile object."""

    class Meta:
        """Define the profile serializer metadata."""

        model = Profile
        fields = ('handle', 'github_access_token')
        extra_kwargs = {'github_access_token': {'write_only': True}}

    def to_representation(self, instance):
        """Provide the serialized representation of the Profile.

        Args:
            instance (Profile): The Profile object to be serialized.

        Returns:
            dict: The serialized Profile.

        """
        return {
            'id': instance.id,
            'handle': instance.handle,
            'github_url': instance.github_url,
            'avatar_url': instance.avatar_url,
            'url': instance.get_relative_url()
        }


@receiver(pre_save, sender=Tip, dispatch_uid="normalize_tip_usernames")
def normalize_tip_usernames(sender, instance, **kwargs):
    """Handle pre-save signals from Tips to normalize Github usernames."""
    if instance.username:
        instance.username = instance.username.replace("@", '')


m2m_changed.connect(m2m_changed_interested, sender=Bounty.interested.through)
# m2m_changed.connect(changed_fulfillments, sender=Bounty.fulfillments)


class UserAction(SuperModel):
    """Records Actions that a user has taken ."""

    ACTION_TYPES = [
        ('Login', 'Login'),
        ('Logout', 'Logout'),
        ('added_slack_integration', 'Added Slack Integration'),
        ('removed_slack_integration', 'Removed Slack Integration'),
        ('updated_avatar', 'Updated Avatar'),
    ]
    action = models.CharField(max_length=50, choices=ACTION_TYPES)
    user = models.ForeignKey(User, related_name='actions', on_delete=models.SET_NULL, null=True)
    profile = models.ForeignKey('dashboard.Profile', related_name='actions', on_delete=models.CASCADE, null=True)
    ip_address = models.GenericIPAddressField(null=True)
    location_data = JSONField(default={})
    metadata = JSONField(default={})

    def __str__(self):
        return f"{self.action} by {self.profile} at {self.created_on}"


class CoinRedemption(SuperModel):
    """Define the coin redemption schema."""

    class Meta:
        """Define metadata associated with CoinRedemption."""

        verbose_name_plural = 'Coin Redemptions'

    shortcode = models.CharField(max_length=255, default='')
    url = models.URLField(null=True)
    network = models.CharField(max_length=255, default='')
    token_name = models.CharField(max_length=255)
    contract_address = models.CharField(max_length=255)
    amount = models.IntegerField(default=1)
    expires_date = models.DateTimeField()


@receiver(pre_save, sender=CoinRedemption, dispatch_uid="to_checksum_address")
def to_checksum_address(sender, instance, **kwargs):
    """Handle pre-save signals from CoinRemptions to normalize the contract address."""
    if instance.contract_address:
        instance.contract_address = Web3.toChecksumAddress(instance.contract_address)
        print(instance.contract_address)


class CoinRedemptionRequest(SuperModel):
    """Define the coin redemption request schema."""

    class Meta:
        """Define metadata associated with CoinRedemptionRequest."""

        verbose_name_plural = 'Coin Redemption Requests'

    coin_redemption = models.OneToOneField(CoinRedemption, blank=False, on_delete=models.CASCADE)
    ip = models.GenericIPAddressField(protocol='IPv4')
    txid = models.CharField(max_length=255, default='')
    txaddress = models.CharField(max_length=255)
    sent_on = models.DateTimeField(null=True)


class Tool(SuperModel):
    """Define the Tool schema."""

    CAT_ADVANCED = 'AD'
    CAT_ALPHA = 'AL'
    CAT_BASIC = 'BA'
    CAT_BUILD = 'BU'
    CAT_COMING_SOON = 'CS'
    CAT_COMMUNITY = 'CO'
    CAT_FOR_FUN = 'FF'
    GAS_TOOLS = "TO"

    TOOL_CATEGORIES = (
        (CAT_ADVANCED, 'advanced'),
        (GAS_TOOLS, 'gas'),
        (CAT_ALPHA, 'alpha'),
        (CAT_BASIC, 'basic'),
        (CAT_BUILD, 'tools to build'),
        (CAT_COMING_SOON, 'coming soon'),
        (CAT_COMMUNITY, 'community'),
        (CAT_FOR_FUN, 'just for fun'),
    )

    name = models.CharField(max_length=255)
    category = models.CharField(max_length=2, choices=TOOL_CATEGORIES)
    img = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    url_name = models.CharField(max_length=40, blank=True)
    link = models.CharField(max_length=255, blank=True)
    link_copy = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=False)
    new = models.BooleanField(default=False)
    stat_graph = models.CharField(max_length=255)
    votes = models.ManyToManyField('dashboard.ToolVote', blank=True)

    def __str__(self):
        return self.name

    @property
    def img_url(self):
        return static(self.img)

    @property
    def link_url(self):
        if self.link and not self.url_name:
            return self.link

        try:
            return reverse(self.url_name)
        except NoReverseMatch:
            pass

        return reverse('tools')

    def starting_score(self):
        if self.category == self.CAT_BASIC:
            return 10
        elif self.category == self.CAT_ADVANCED:
            return 5
        elif self.category in [self.CAT_BUILD, self.CAT_COMMUNITY]:
            return 3
        elif self.category == self.CAT_ALPHA:
            return 2
        elif self.category == self.CAT_COMING_SOON:
            return 1
        elif self.category == self.CAT_FOR_FUN:
            return 1
        return 0

    def vote_score(self):
        score = self.starting_score()
        for vote in self.votes.all():
            score += vote.value
        return score

    def i18n_name(self):
        return _(self.name)

    def i18n_description(self):
        return _(self.description)

    def i18n_link_copy(self):
        return _(self.link_copy)


class ToolVote(models.Model):
    """Define the vote placed on a tool."""

    profile = models.ForeignKey('dashboard.Profile', related_name='votes', on_delete=models.CASCADE)
    value = models.IntegerField(default=0)

    @property
    def tool(self):
        try:
            return Tool.objects.filter(votes__in=[self.pk]).first()
        except Exception:
            return None

    def __str__(self):
        return f"{self.profile} | {self.value} | {self.tool}"


class TokenApproval(SuperModel):
    """A token approval."""

    profile = models.ForeignKey('dashboard.Profile', related_name='token_approvals', on_delete=models.CASCADE)
    coinbase = models.CharField(max_length=50)
    token_name = models.CharField(max_length=50)
    token_address = models.CharField(max_length=50)
    approved_address = models.CharField(max_length=50)
    approved_name = models.CharField(max_length=50)
    tx = models.CharField(max_length=255, default='')
    network = models.CharField(max_length=255, default='')

    def __str__(self):
        return f"{self.coinbase} | {self.token_name} | {self.profile}"

    @property
    def coinbase_short(self):
        coinbase_short = f"{self.coinbase[0:5]}...{self.coinbase[-4:]}"
        return coinbase_short
