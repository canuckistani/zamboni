# -*- coding: utf-8 -*-
import calendar
import json
import time
from urllib import urlencode
import urlparse
import uuid

from django.conf import settings
from django.core.urlresolvers import NoReverseMatch
from django.db import models
from django.dispatch import receiver

import commonware.log
from tower import ugettext as _

from access import acl
import amo
from amo.decorators import skip_cache
from amo.helpers import absolutify
import amo.models
from amo.urlresolvers import reverse
from amo.utils import memoize
from addons import query
from addons.models import (Addon, AddonDeviceType, update_name_table,
                           update_search_index)
from bandwagon.models import Collection
from files.models import FileUpload, Platform
from lib.crypto.receipt import sign
from versions.models import Version

import jwt


log = commonware.log.getLogger('z.addons')


class WebappManager(amo.models.ManagerBase):

    def __init__(self, include_deleted=False):
        amo.models.ManagerBase.__init__(self)
        self.include_deleted = include_deleted

    def get_query_set(self):
        qs = super(WebappManager, self).get_query_set()
        qs = qs._clone(klass=query.IndexQuerySet).filter(type=amo.ADDON_WEBAPP)
        if not self.include_deleted:
            qs = qs.exclude(status=amo.STATUS_DELETED)
        return qs.transform(Webapp.transformer)

    def valid(self):
        return self.filter(status__in=amo.LISTED_STATUSES,
                           disabled_by_user=False)

    def reviewed(self):
        return self.filter(status__in=amo.REVIEWED_STATUSES)

    def visible(self):
        return self.filter(status=amo.STATUS_PUBLIC, disabled_by_user=False)

    def top_free(self, listed=True):
        qs = self.visible() if listed else self
        return (qs.filter(premium_type__in=amo.ADDON_FREES)
                .exclude(addonpremium__price__price__isnull=False)
                .order_by('-weekly_downloads')
                .with_index(addons='downloads_type_idx'))

    def top_paid(self, listed=True):
        qs = self.visible() if listed else self
        return (qs.filter(premium_type__in=amo.ADDON_PREMIUMS,
                          addonpremium__price__price__gt=0)
                .order_by('-weekly_downloads')
                .with_index(addons='downloads_type_idx'))

    @skip_cache
    def pending(self):
        # - Holding
        # ** Approved   -- PUBLIC
        # ** Unapproved -- PENDING
        # - Open
        # ** Reviewed   -- PUBLIC
        # ** Unreviewed -- LITE
        # ** Rejected   -- REJECTED
        return self.filter(status=amo.WEBAPPS_UNREVIEWED_STATUS)


# We use super(Addon, self) on purpose to override expectations in Addon that
# are not true for Webapp. Webapp is just inheriting so it can share the db
# table.
class Webapp(Addon):

    objects = WebappManager()
    with_deleted = WebappManager(include_deleted=True)

    class Meta:
        proxy = True

    def save(self, **kw):
        # Make sure we have the right type.
        self.type = amo.ADDON_WEBAPP
        self.clean_slug(slug_field='app_slug')
        creating = not self.id
        super(Addon, self).save(**kw)
        if creating:
            # Set the slug once we have an id to keep things in order.
            self.update(slug='app-%s' % self.id)

    @staticmethod
    def transformer(apps):
        # I think we can do less than the Addon transformer, so at some point
        # we'll want to copy that over.
        apps_dict = Addon.transformer(apps)
        if not apps_dict:
            return

        for adt in AddonDeviceType.objects.filter(addon__in=apps_dict):
            if not getattr(apps_dict[adt.addon_id], '_device_types', None):
                apps_dict[adt.addon_id]._device_types = []
            apps_dict[adt.addon_id]._device_types.append(adt.device_type)

        # TODO: This may need to be its own column.
        for app in apps:
            if not hasattr(app, '_rating_counts') and hasattr(app, '_ratings'):
                app._rating_counts = app.rating_counts

    def get_url_path(self, more=False, add_prefix=True):
        # We won't have to do this when Marketplace absorbs all apps views,
        # but for now pretend you didn't see this.
        try:
            return reverse('detail', args=[self.app_slug],
                           add_prefix=add_prefix)
        except NoReverseMatch:
            # Fall back to old details page until the views get ported.
            return super(Webapp, self).get_url_path(more=more,
                                                    add_prefix=add_prefix)

    def get_detail_url(self, action=None):
        """Reverse URLs for 'detail', 'details.record', etc."""
        return reverse(('detail.%s' % action) if action else 'detail',
                       args=[self.app_slug])

    def get_purchase_url(self, action=None, args=None):
        """Reverse URLs for 'purchase', 'purchase.done', etc."""
        return reverse(('purchase.%s' % action) if action else 'purchase',
                       args=[self.app_slug] + (args or []))

    def get_dev_url(self, action='edit', args=None, prefix_only=False):
        # Either link to the "new" Marketplace Developer Hub or the old one.
        args = args or []
        prefix = ('mkt.developers' if getattr(settings, 'MARKETPLACE', False)
                  else 'devhub')
        view_name = ('%s.%s' if prefix_only else '%s.apps.%s')
        return reverse(view_name % (prefix, action),
                       args=[self.app_slug] + args)

    def get_ratings_url(self, action='list', args=None, add_prefix=True):
        """Reverse URLs for 'ratings.list', 'ratings.add', etc."""
        return reverse(('ratings.%s' % action),
                       args=[self.app_slug] + (args or []),
                       add_prefix=add_prefix)

    def get_stats_url(self, action='overview', args=None):
        """Reverse URLs for 'stats', 'stats.overview', etc."""
        return reverse(('mkt.stats.%s' % action),
                       args=[self.app_slug] + (args or []))

    @staticmethod
    def domain_from_url(url):
        if not url:
            raise ValueError('URL was empty')
        hostname = urlparse.urlparse(url).hostname
        if hostname:
            hostname = hostname.lower()
            if hostname.startswith('www.'):
                hostname = hostname[4:]
        return hostname

    @property
    def device_types(self):
        # If the transformer attached something, use it.
        if hasattr(self, '_device_types'):
            return self._device_types
        return [d.device_type for d in
                self.addondevicetype_set.order_by('device_type__id')]

    @property
    def rating_counts(self):
        # If the transformer attached something, use it.
        if hasattr(self, '_rating_counts'):
            return self._rating_counts
        scores = dict(self._ratings.values_list('score')
                          .annotate(models.Count('id')))
        positive, negative = scores.get(1, 0), scores.get(-1, 0)
        return {'total': positive + negative,
                'positive': positive,
                'negative': negative}

    @property
    def origin(self):
        parsed = urlparse.urlparse(self.manifest_url)
        return '%s://%s' % (parsed.scheme, parsed.netloc)

    def get_latest_file(self):
        """Get the latest file from the current version."""
        cur = self.current_version
        if cur:
            res = cur.files.order_by('-created')
            if res:
                return res[0]

    def has_icon_in_manifest(self):
        data = self.get_manifest_json()
        return 'icons' in data

    def get_manifest_json(self):
        try:
            # The first file created for each version of the web app
            # is the manifest.
            with open(self.get_latest_file().file_path, 'r') as mf:
                return json.load(mf)
        except Exception, e:
            log.error('Failed to open saved manifest %r for webapp %s, %s.'
                      % (self.manifest_url, self.pk, e))
            raise

    def share_url(self):
        return reverse('apps.share', args=[self.app_slug])

    def manifest_updated(self, manifest):
        """The manifest has updated, create a version and file."""
        with open(manifest) as fh:
            chunks = fh.read()

        # We'll only create a file upload when we detect that the manifest
        # has changed, otherwise we'll be creating an awful lot of these.
        upload = FileUpload.from_post(chunks, manifest, len(chunks))
        # This does most of the heavy work.
        Version.from_upload(upload, self,
                            [Platform.objects.get(id=amo.PLATFORM_ALL.id)])
        # Triggering this ensures that the current_version gets updated.
        self.update_version()
        amo.log(amo.LOG.MANIFEST_UPDATED, self)

    def mark_done(self):
        """When the submission process is done, update status accordingly."""
        self.update(status=amo.WEBAPPS_UNREVIEWED_STATUS)

    def authors_other_addons(self, app=None):
        """Return other apps by the same author."""
        return (self.__class__.objects.visible()
                              .filter(type=amo.ADDON_WEBAPP)
                              .exclude(id=self.id).distinct()
                              .filter(addonuser__listed=True,
                                      authors__in=self.listed_authors))

    def can_purchase(self):
        return self.is_premium() and self.premium and self.is_public()

    def is_purchased(self, user):
        return user and self.id in user.purchase_ids()

    def is_pending(self):
        return self.status == amo.STATUS_PENDING

    def get_price(self):
        if self.is_premium() and self.premium:
            return self.premium.get_price_locale()
        return _(u'FREE')

    @amo.cached_property
    def promo(self):
        return self.get_promo()

    def get_promo(self):
        try:
            return self.previews.filter(position=-1)[0]
        except IndexError:
            pass

    @classmethod
    def featured_collection(cls, group):
        try:
            featured = Collection.objects.get(author__username='mozilla',
                                              slug='featured_apps_%s' % group,
                                              type=amo.COLLECTION_FEATURED)
        except Collection.DoesNotExist:
            featured = None
        return featured

    @classmethod
    def featured(cls, group):
        featured = cls.featured_collection(group)
        if featured:
            return (featured.addons.filter(status=amo.STATUS_PUBLIC,
                                           disabled_by_user=False)
                    .order_by('-weekly_downloads'))
        else:
            return cls.objects.none()

    @classmethod
    def from_search(cls):
        return cls.search().filter(type=amo.ADDON_WEBAPP,
                                   status=amo.STATUS_PUBLIC,
                                   is_disabled=False)

    @classmethod
    def popular(cls):
        """Elastically grab the most popular apps."""
        return cls.from_search().order_by('-weekly_downloads')

    @classmethod
    def latest(cls):
        """Elastically grab the most recent apps."""
        return cls.from_search().order_by('-created')


# Pull all translated_fields from Addon over to Webapp.
Webapp._meta.translated_fields = Addon._meta.translated_fields


models.signals.post_save.connect(update_search_index, sender=Webapp,
                                 dispatch_uid='mkt.webapps.index')
models.signals.post_save.connect(update_name_table, sender=Webapp,
                                 dispatch_uid='mkt.webapps.update.name.table')


class Installed(amo.models.ModelBase):
    """Track WebApp installations."""
    addon = models.ForeignKey('addons.Addon', related_name='installed')
    user = models.ForeignKey('users.UserProfile')
    uuid = models.CharField(max_length=255, db_index=True, unique=True)
    # Because the addon could change between free and premium,
    # we need to store the state at time of install here.
    premium_type = models.PositiveIntegerField(
                                    choices=amo.ADDON_PREMIUM_TYPES.items(),
                                    null=True, default=None)

    class Meta:
        db_table = 'users_install'
        unique_together = ('addon', 'user')


@receiver(models.signals.post_save, sender=Installed)
def add_uuid(sender, **kw):
    if not kw.get('raw'):
        install = kw['instance']
        if not install.uuid and install.premium_type == None:
            install.uuid = ('%s-%s' % (install.pk, str(uuid.uuid4())))
            install.premium_type = install.addon.premium_type
            install.save()


@memoize(prefix='create-receipt', time=60 * 10)
def create_receipt(installed_pk, flavour=None):
    assert flavour in [None, 'author', 'reviewer'], (
           'Invalid flavour: %s' % flavour)

    installed = Installed.objects.get(pk=installed_pk)
    addon_pk = installed.addon.pk
    time_ = calendar.timegm(time.gmtime())
    product = {'url': installed.addon.origin,
               'storedata': urlencode({'id': int(addon_pk)})}

    # Generate different receipts for reviewers or authors.
    if flavour in ['author', 'reviewer']:
        if not (acl.action_allowed_user(installed.user, 'Apps', 'Review') or
                installed.addon.has_author(installed.user)):
            raise ValueError('User %s is not a reviewer or author' %
                             installed.user.pk)

        expiry = time_ + (60 * 60 * 24)
        product['type'] = flavour
        verify = absolutify(reverse('reviewers.receipt.verify',
                                    args=[installed.addon.app_slug]))
    else:
        expiry = time_ + settings.WEBAPPS_RECEIPT_EXPIRY_SECONDS
        verify = '%s%s' % (settings.WEBAPPS_RECEIPT_URL, addon_pk)

    detail = reverse('account.purchases.receipt', args=[addon_pk])
    reissue = installed.addon.get_purchase_url('reissue')
    receipt = dict(detail=absolutify(detail), exp=expiry, iat=time_,
                   iss=settings.SITE_URL, nbf=time_, product=product,
                   reissue=absolutify(reissue), typ='purchase-receipt',
                   user={'type': 'directed-identifier',
                         'value': installed.uuid},
                   verify=absolutify(verify))

    if settings.SIGNING_SERVER_ACTIVE:
        # The shiny new code.
        return sign(receipt)
    else:
        # Our old bad code.
        return jwt.encode(receipt, get_key(), u'RS512')


def get_key():
    """Return a key for using with encode."""
    return jwt.rsa_load(settings.WEBAPPS_RECEIPT_KEY)
