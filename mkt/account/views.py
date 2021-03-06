from datetime import datetime, timedelta

from django import http
from django.shortcuts import get_object_or_404, redirect

import commonware.log
from commonware.response.decorators import xframe_allow
import jingo
from tower import ugettext as _
import waffle

from access import acl
import amo
from amo.decorators import (login_required, permission_required, post_required,
                            write)
from amo.urlresolvers import reverse
from amo.utils import paginate
from market.models import PreApprovalUser
import paypal
from users.models import UserProfile
from users.tasks import delete_photo as delete_photo_task
from users.views import logout
from mkt.account.forms import CurrencyForm
from mkt.site import messages
from . import forms
from .utils import purchase_list

log = commonware.log.getLogger('mkt.account')
paypal_log = commonware.log.getLogger('mkt.paypal')


@write
@login_required
@xframe_allow
def payment(request, status=None):
    # Note this is not post required, because PayPal does not reply with a
    # POST but a GET, that's a sad face.
    pre, created = (PreApprovalUser.objects
                                   .safer_get_or_create(user=request.amo_user))

    context = {'preapproval': pre,
               'currency': CurrencyForm(initial={'currency':
                                                 pre.currency or 'USD'})}

    if status:
        data = request.session.get('setup-preapproval', {})

        context['status'] = status

        if status == 'complete':
            # The user has completed the setup at PayPal and bounced back.
            if 'setup-preapproval' in request.session:
                paypal_log.info(u'Preapproval key created for user: %s, %s.' %
                                (request.amo_user.pk, data['key'][:5]))
                amo.log(amo.LOG.PREAPPROVAL_ADDED)
                pre.update(paypal_key=data.get('key'),
                           paypal_expiry=data.get('expiry'))

                # If there is a target, bounce to it and don't show a message
                # we'll let whatever set this up worry about that.
                if data.get('complete'):
                    return redirect(data['complete'])

                messages.success(request,
                    _("You're all set for instant app purchases with PayPal."))
                del request.session['setup-preapproval']

        elif status == 'cancel':
            # The user has chosen to cancel out of PayPal. Nothing really
            # to do here, PayPal just bounce to the cancel page if defined.
            if data.get('cancel'):
                return redirect(data['cancel'])

            messages.success(request,
                _('Your payment pre-approval has been cancelled.'))

        elif status == 'remove':
            # The user has an pre approval key set and chooses to remove it
            if pre.paypal_key:
                pre.update(paypal_key='')
                amo.log(amo.LOG.PREAPPROVAL_REMOVED)
                messages.success(request,
                    _('Your payment pre-approval has been disabled.'))
                paypal_log.info(u'Preapproval key removed for user: %s'
                                % request.amo_user)

    return jingo.render(request, 'account/payment.html', context)


@write
@post_required
@login_required
def currency(request, do_redirect=True):
    pre, created = (PreApprovalUser.objects
                        .safer_get_or_create(user=request.amo_user))
    currency = CurrencyForm(request.POST or {},
                            initial={'currency': pre.currency or 'USD'})
    if currency.is_valid():
        pre.update(currency=currency.cleaned_data['currency'])
        if do_redirect:
            messages.success(request, _('Currency saved.'))
            amo.log(amo.LOG.CURRENCY_UPDATED)
            return redirect(reverse('account.payment'))
    else:
        return jingo.render(request, 'account/payment.html',
                            {'preapproval': pre,
                             'currency': currency})


@write
@login_required
def preapproval(request, complete=None, cancel=None):
    if waffle.switch_is_active('currencies'):
        failure = currency(request, do_redirect=False)
        if failure:
            return failure

    today = datetime.today()
    data = {'startDate': today,
            'endDate': today + timedelta(days=365),
            'pattern': 'account.payment',
            }
    try:
        result = paypal.get_preapproval_key(data)
    except paypal.PaypalError, e:
        paypal_log.error(u'Preapproval key: %s' % e, exc_info=True)
        raise

    paypal_log.info(u'Got preapproval key for user: %s, %s...' %
                    (request.amo_user.pk, result['preapprovalKey'][:5]))
    request.session['setup-preapproval'] = {
        'key': result['preapprovalKey'],
        'expiry': data['endDate'],
        'complete': complete,
        'cancel': cancel,
    }

    url = paypal.get_preapproval_url(result['preapprovalKey'])
    return redirect(url)


def purchases(request, product_id=None, template=None):
    """A list of purchases that a user has made through the Marketplace."""
    products, contributions, listing = purchase_list(request,
                                                     request.amo_user,
                                                     product_id)
    return jingo.render(request, 'account/purchases.html',
                        {'pager': products,
                         'listing_filter': listing,
                         'contributions': contributions,
                         'single': bool(product_id),
                         'show_link': True})


@write
def account_settings(request):
    # Don't use `request.amo_user` because it's too cached.
    amo_user = request.amo_user.user.get_profile()
    form = forms.UserEditForm(request.POST or None, request.FILES or None,
                              request=request, instance=amo_user)
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, _('Profile Updated'))
            amo.log(amo.LOG.USER_EDITED)
            return redirect('account.settings')
        else:
            messages.form_errors(request)
    return jingo.render(request, 'account/settings.html',
                        {'form': form, 'amouser': amo_user})


@write
@login_required
@permission_required('Users', 'Edit')
def admin_edit(request, user_id):
    amouser = get_object_or_404(UserProfile, pk=user_id)
    form = forms.AdminUserEditForm(request.POST or None, request.FILES or None,
                                   request=request, instance=amouser)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Profile Updated'))
        return redirect('zadmin.index')
    return jingo.render(request, 'account/settings.html',
                        {'form': form, 'amouser': amouser})


def delete(request):
    amouser = request.amo_user
    form = forms.UserDeleteForm(request.POST or None, request=request)
    if request.method == 'POST' and form.is_valid():
        messages.success(request, _('Profile Deleted'))
        amouser.anonymize()
        logout(request)
        form = None
        return redirect('users.login')
    return jingo.render(request, 'account/delete.html',
                        {'form': form, 'amouser': amouser})


@post_required
def delete_photo(request):
    request.amo_user.update(picture_type='')
    delete_photo_task.delay(request.amo_user.picture_path)
    log.debug(u'User (%s) deleted photo' % request.amo_user)
    messages.success(request, _('Photo Deleted'))
    amo.log(amo.LOG.USER_EDITED)
    return http.HttpResponse()


def profile(request, username):
    if username.isdigit():
        user = get_object_or_404(UserProfile, id=username)
    else:
        user = get_object_or_404(UserProfile, username=username)

    edit_any_user = acl.action_allowed(request, 'Users', 'Edit')
    own_profile = (request.user.is_authenticated() and
                   request.amo_user.id == user.id)

    submissions = []
    if user.is_developer:
        submissions = paginate(request,
                               user.apps_listed.order_by('-weekly_downloads'),
                               per_page=5)

    data = {'profile': user, 'edit_any_user': edit_any_user,
            'submissions': submissions, 'own_profile': own_profile}

    return jingo.render(request, 'account/profile.html', data)
