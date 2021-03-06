try:
    from urllib import parse as urllib_parse
except ImportError:     # Python 2
    import urllib as urllib_parse
    import urlparse
    urllib_parse.urlparse = urlparse.urlparse

from django.contrib.auth.models import User
from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.template import RequestContext
from django.template.response import TemplateResponse
from django.views.generic.edit import UpdateView, FormView
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect, resolve_url, render_to_response
from django.views.generic import RedirectView, TemplateView
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login as auth_login, logout as auth_logout, get_user_model
from django.utils.translation import ugettext as _
from django.db.models import Count
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.csrf import csrf_exempt
from django.contrib.sites.models import get_current_site
from django.conf import settings

from guardian.shortcuts import get_users_with_perms, assign_perm, remove_perm
from registration.backends import get_backend
from follow.models import Follow

from versionutils.versioning.utils import is_versioned
from regions.models import Region
from regions import get_main_region
from regions.views import RegionMixin, RegionAdminRequired
from localwiki.utils.urlresolvers import reverse
from localwiki.utils.cache import cache_page

from .templatetags.user_tags import user_link
from .models import UserProfile
from .forms import UserSetForm, UserSettingsForm, DeactivateForm


def humanize_int(n):
    mag = 0
    if n < 1000:
        return str(n)
    while n>= 1000:
        mag += 1
        n /= 1000.0
    return '%.1f%s' % (n, ['', 'k', 'M', 'B', 'T', 'P'][mag])


def pretty_url(url):
    if urllib_parse.urlparse(url).path == '/':
        # Strip trailing slash
        url = url[:-1]
    if url.startswith('http://'):
        url = url[7:]
    elif url.startswith('https://'):
        url = url[8:]
    return url


def get_user_page(user, request):
    """
    Hacky heuristics for picking the underlying Page that holds the userpage content.

    TODO: Make this all belong the a single administrative region, 'users', once we 
          have a notifications framework in place.
    """
    from pages.models import Page, slugify

    pagename = "Users/%s" % user.username
    user_pages = Page.objects.filter(slug=slugify(pagename))
    if user_pages:
        # Just pick the first one
        return user_pages[0]
    else:
        # Check to see if they've edited a region recently
        edited_pages = Page.versions.filter(version_info__user=user)
        referer = request.META.get('HTTP_REFERER')
        if edited_pages.exists():
            region = edited_pages[0].region
            return Page(name=pagename, region=region)
        # Let's try and guess by the previous URL. Ugh!
        if referer:
            urlparts = urllib_parse.urlparse(referer)
            # Is this host us?
            for host in settings.ALLOWED_HOSTS:
                if urlparts.netloc.endswith(host):
                    pathparts = parts.path.split('/')
                    # Is the path in a region?
                    if len(pathparts) > 1 and Region.objects.filter(slug=pathparts[1]).exists():
                        return Page(name=pagename, region=Region.objects.get(slug=pathparts[1]))

        # Put it in the main region for now :/
        return Page(name=pagename, region=get_main_region())


class UserPageView(TemplateView):
    template_name = 'users/user_page.html'

    def get_context_data(self, **kwargs):
        from pages.models import Page, PageFile
        from maps.models import MapData
        from tags.models import PageTagSet

        context = super(UserPageView, self).get_context_data(**kwargs)

        username = self.kwargs.get('username')
        user = get_object_or_404(User, username__iexact=username)
        profile = getattr(user, 'userprofile', None)
        
        #########################
        # Calculate user stats
        #########################
        page_edits = Page.versions.filter(version_info__user=user).count()
        map_edits = MapData.versions.filter(version_info__user=user).count()
        tag_edits = PageTagSet.versions.filter(version_info__user=user).count()
        file_edits = PageFile.versions.filter(version_info__user=user).count()

        # Total contributions across data types
        num_contributions = page_edits + map_edits + tag_edits + file_edits

        # Total 'pages touched'
        num_pages_edited = Page.versions.filter(version_info__user=user).values('slug').distinct().count()

        # Total 'maps touched'
        num_maps_edited = MapData.versions.filter(version_info__user=user).values('page__slug').distinct().count()

        # Regions followed
        regions_followed = Follow.objects.filter(user=user).exclude(target_region=None)

        # Users, pages followed
        num_pages_followed = Follow.objects.filter(user=user).exclude(target_page=None).count()
        num_users_followed = Follow.objects.filter(user=user).exclude(target_user=None).exclude(target_user=user).count()

        context['user_for_page'] = user
        context['pretty_personal_url'] = pretty_url(user.userprofile.personal_url) if user.userprofile.personal_url else None
        context['page'] = get_user_page(user, self.request)
        context['num_contributions'] = humanize_int(num_contributions)
        context['num_pages_edited'] = humanize_int(num_pages_edited)
        context['num_maps_edited'] = humanize_int(num_maps_edited)
        context['regions_followed'] = regions_followed
        context['num_pages_followed'] = num_pages_followed
        context['num_users_followed'] = num_users_followed

        return context


class GlobalUserpageRedirectView(RedirectView):
    permanent = True

    def get_redirect_url(self, **kwargs):
        username = kwargs.get('username', None)
        rest = kwargs.get('rest', '')
        if rest:
            slug = "%s%s" % (username , rest)
            return reverse('user-page-content-view', kwargs={'slug': slug})
        else:
            return reverse('user-page', kwargs={'username': username})
        return 


class SetPermissionsView(RegionAdminRequired, RegionMixin, FormView):
    form_class = UserSetForm

    def get_initial(self):
        # Technically, this returns all users with *any* permissions
        # on the object, but for our purposes this is okay.
        users = get_users_with_perms(self.get_object())
        if users:
            everyone_or_user_set = 'just_these_users'
        else:
            everyone_or_user_set = 'everyone'
        return {'users': users, 'everyone_or_user_set': everyone_or_user_set}

    def get_object_type(self, obj):
        return obj.__class__.__name__.lower()

    def get_form_kwargs(self):
        kwargs = super(SetPermissionsView, self).get_form_kwargs()
        # We need to pass the `region` to the UserSetForm.
        kwargs['region'] = self.get_region()
        kwargs['this_user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        response = super(SetPermissionsView, self).form_valid(form)

        obj = self.get_object()
        obj_type = self.get_object_type(obj)

        users_old = set(get_users_with_perms(self.get_object()))
        users = set(form.cleaned_data['users'])

        if form.cleaned_data['who_can_change'] == 'everyone':
            # Clear out all users
            del_users = users_old
            add_users = []
        else:
            del_users = users_old - users
            add_users = users - users_old

        for u in add_users:
            assign_perm('change_%s' % obj_type, u, obj)
            assign_perm('add_%s' % obj_type, u, obj)
            assign_perm('delete_%s' % obj_type, u, obj)

        for u in del_users:
            remove_perm('change_%s' % obj_type, u, obj)
            remove_perm('add_%s' % obj_type, u, obj)
            remove_perm('delete_%s' % obj_type, u, obj)

        messages.add_message(self.request, messages.SUCCESS, _("Permissions updated!"))
        return response


class UserSettingsView(UpdateView):
    template_name = 'users/settings.html'
    model = UserProfile
    form_class = UserSettingsForm

    def get_object(self):
        if not self.request.user.is_authenticated():
            raise PermissionDenied(_("You must be logged in to change user settings."))
        return UserProfile.objects.get(user=self.request.user)

    def form_valid(self, form):
        response = super(UserSettingsView, self).form_valid(form)

        userprofile = self.get_object()
        userprofile.user.email = form.cleaned_data['email']
        userprofile.user.name = form.cleaned_data['name']
        if form.cleaned_data['gravatar_email'] != userprofile.user.email:
            userprofile._gravatar_email = form.cleaned_data['gravatar_email']
        userprofile.user.save()
        userprofile.save()

        messages.add_message(self.request, messages.SUCCESS, _("User settings updated!"))
        return response

    def get_initial(self):
        initial = super(UserSettingsView, self).get_initial()

        initial['name'] = self.object.user.name
        initial['email'] = self.object.user.email
        initial['gravatar_email'] = self.object.gravatar_email
        return initial

    def get_success_url(self):
        return self.request.user.get_absolute_url()


class UserDeactivateView(FormView):
    template_name = 'users/deactivate.html'
    form_class = DeactivateForm 

    def get_context_data(self, **kwargs):
        if not self.request.user.is_authenticated():
            raise PermissionDenied(_("You must be logged in to change user settings."))
        return super(UserDeactivateView, self).get_context_data(**kwargs)

    def get_initial(self):
        return {}

    def form_valid(self, form):
        response = super(UserDeactivateView, self).form_valid(form)

        if not self.request.user.is_authenticated():
            raise PermissionDenied(_("You must be logged in to change user settings."))

        self.request.user.is_active = not form.cleaned_data['disabled']
        self.request.user.save()

        logout(self.request)
        messages.add_message(self.request, messages.SUCCESS, _("Your account has been de-activated."))

        return response

    def get_success_url(self):
        return '/'


class AddContributorsMixin(object):
    """
    Add the editors of this object to the view's context as
    `contributors_html` and `contributors_number`.
    """
    def get_context_data(self, **kwargs):
        context = super(AddContributorsMixin, self).get_context_data(**kwargs)
        obj = self.get_object()

        if not is_versioned(obj):
            return context

        users_by_edit_count = obj.versions.exclude(history_user__isnull=True).order_by('history_user').values('history_user').annotate(nedits=Count('history_user')).order_by('-nedits')
        top_3 = users_by_edit_count[:3]
        num_rest = len(users_by_edit_count[3:])

        top_3_html = ''
        for u_info in top_3:
            user = User.objects.get(pk=u_info['history_user'])
            top_3_html += user_link(user, region=self.get_region(), show_username=False, size=24)

        context['contributors_html'] = top_3_html
        context['contributors_number'] = num_rest
    
        return context


def suggest_users(request, region=None):
    """
    Simple users suggest.
    """
    # XXX TODO: Break this out when doing more API work.
    import json

    term = request.GET.get('term', None)
    if not term:
        return HttpResponse('')
    results = User.objects.filter(
        username__istartswith=term)
    results = [t.username for t in results]
    return HttpResponse(json.dumps(results))


def is_safe_url(url):
    """
    Return ``True`` if the url is a safe redirection (i.e. it doesn't point to
    a non-allowed hostname and uses a safe scheme).

    Always returns ``False`` on an empty url.
    """
    # XXX TODO something like this should probably be integrated into Django.
    # Report a bug for contrib.auth.views.is_safe_url.
    if not url:
        return False
    url_info = urllib_parse.urlparse(url)
    return (not url_info.netloc or url_info.netloc in settings.XSESSION_DOMAINS) and \
        (not url_info.scheme or url_info.scheme in ['http', 'https'])


def get_registration_success_url(request):
    if request.GET.get('post_save', None):
        if request.GET.get('post_save') == 'create_region':
            return reverse('frontpage', kwargs={'region': request.GET.get('slug')})
    return None


def process_post_save_after_auth(request):
    from django.contrib.gis.geos import GEOSGeometry
    from regions.views import create_region
    if request.GET.get('post_save') == 'create_region':
        slug = request.GET.get('slug')
        full_name = request.GET.get('full_name')
        geom = GEOSGeometry(request.GET.get('geom'))
        default_language = request.GET.get('default_language')
        if slug and full_name and geom and default_language:
            create_region(
                request,
                slug=slug,
                full_name=full_name,
                geom=geom,
                default_language=default_language
            )

@cache_page(60 * 60 * 24)
@csrf_exempt
def register(request, backend, success_url=None, form_class=None,
             disallowed_url='registration_disallowed',
             template_name='registration/registration_form.html',
             extra_context=None):
    backend = get_backend(backend)
    if not backend.registration_allowed(request):
        return redirect(disallowed_url)
    if form_class is None:
        form_class = backend.get_form_class(request)

    if request.method == 'POST':
        form = form_class(data=request.POST, files=request.FILES)
        if form.is_valid():
            new_user = backend.register(request, **form.cleaned_data)

            ##########################################
            # Our custom bit here:
            ##########################################
            if request.GET.get('post_save', None):
                process_post_save_after_auth(request)

            if success_url is None:
                to, args, kwargs = backend.post_registration_redirect(request, new_user)
                return redirect(to, *args, **kwargs)
            else:
                return redirect(success_url)
    else:
        form = form_class()
    
    if extra_context is None:
        extra_context = {}
    context = RequestContext(request)
    for key, value in extra_context.items():
        context[key] = callable(value) and value() or value

    return render_to_response(template_name,
                              {'form': form},
                              context_instance=context)


@cache_page(60 * 60 * 24)
@csrf_exempt
@sensitive_post_parameters()
def login(request, template_name='registration/login.html',
          redirect_field_name=REDIRECT_FIELD_NAME,
          authentication_form=AuthenticationForm,
          current_app=None, extra_context=None):
    """
    Displays the login form and handles the login action.

    A copy/paste of django.contrib.auth.views.login, except with
    a different is_safe_url() check.
    """
    # XXX TODO: Replace this function with something that's
    # more flexible - class based auth?

    redirect_to = request.REQUEST.get(redirect_field_name, '')

    if request.user.is_authenticated():
        # Not valid
        return HttpResponseRedirect('/')

    if request.method == "POST":
        form = authentication_form(data=request.POST)
        if form.is_valid():

            # Ensure the user-originating redirection url is safe.
            if not is_safe_url(redirect_to):
                redirect_to = resolve_url(settings.LOGIN_REDIRECT_URL)

            # Okay, security check complete. Log the user in.
            auth_login(request, form.get_user())

            if request.session.test_cookie_worked():
                request.session.delete_test_cookie()

            ##############################################
            # Custom post-login action here:
            ##############################################
            if request.GET.get('post_save', None):
                process_post_save_after_auth(request)

            return HttpResponseRedirect(redirect_to)
    else:
        form = authentication_form(request)

    current_site = get_current_site(request)

    context = {
        'form': form,
        redirect_field_name: redirect_to,
        'site': current_site,
        'site_name': current_site.name,
    }
    if extra_context is not None:
        context.update(extra_context)

    return TemplateResponse(request, template_name, context,
                            current_app=current_app)


def logout(request, next_page=None,
           template_name='registration/logged_out.html',
           redirect_field_name=REDIRECT_FIELD_NAME,
           current_app=None, extra_context=None):
    """
    Logs out the user and displays 'You are logged out' message.

    A copy/paste of django.contrib.auth.views.logout, except with
    a different is_safe_url() check.
    """
    # XXX TODO: Replace this function with something that's
    # more flexible - class based auth?

    auth_logout(request)

    if redirect_field_name in request.REQUEST:
        next_page = request.REQUEST[redirect_field_name]
        # Security check -- don't allow redirection to a different host.
        if not is_safe_url(next_page):
            next_page = '/'

    if next_page:
        # Redirect to this page until the session has been cleared.
        return HttpResponseRedirect(next_page)

    current_site = get_current_site(request)
    context = {
        'site': current_site,
        'site_name': current_site.name,
        'title': _('Logged out')
    }
    if extra_context is not None:
        context.update(extra_context)
    return TemplateResponse(request, template_name, context,
        current_app=current_app)
