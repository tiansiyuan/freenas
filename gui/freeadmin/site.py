from functools import update_wrapper

import hashlib
import json
import logging
import re

from django.conf.urls import url, include
from django.core.urlresolvers import reverse
from django.db.models.base import ModelBase
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from freenasUI.common.system import get_sw_name, get_sw_year, get_sw_version
from freenasUI.freeadmin.apppool import appPool
from freenasUI.freeadmin.options import BaseFreeAdmin

RE_ALERT = re.compile(r'^(?P<status>\w+)\[(?P<msgid>.+?)\]: (?P<message>.+)')
log = logging.getLogger('freeadmin.site')


class NotRegistered(Exception):
    pass


class FreeAdminSite(object):

    def __init__(self):
        self._registry = {}

    def register(
        self, model_or_iterable, admin_class=None, freeadmin=None, **options
    ):
        """
        Registers the given model(s) with the given admin class.

        The model(s) should be Model classes, not instances.

        If an admin class isn't given, it will use BaseFreeAdmin (default
        admin options). If keyword arguments are given they'll be applied
        as options to the admin class.
        """
        if not admin_class:
            admin_class = BaseFreeAdmin

        if isinstance(model_or_iterable, ModelBase):
            model_or_iterable = [model_or_iterable]
        admins = []

        if model_or_iterable is None:
            admin_obj = admin_class(c=freeadmin, admin=self)
            self._registry[admin_obj] = admin_obj
        else:
            for model in model_or_iterable:
                # FIXME: Do not allow abstract models expect for the ones
                #       In a whitelist
                # if model._meta.abstract:
                #    log.warn(
                #        "Model %r is abstract and thus cannot be registered",
                #        model)
                #    return None
                if model in self._registry:
                    log.debug(
                        "Model %r already registered, overwriting...",
                        model)

                # If we got **options then dynamically construct a subclass of
                # admin_class with those **options.
                if options:
                    # For reasons I don't quite understand, without a __module_
                    # the created class appears to "live" in the wrong place,
                    # which causes issues later on.
                    options['__module__'] = __name__
                    admin_class = type(
                        "%sAdmin" % model.__name__,
                        (admin_class, ),
                        options
                    )

                # Instantiate the admin class to save in the registry
                admin_obj = admin_class(c=freeadmin, model=model, admin=self)
                self._registry[model] = admin_obj
                model.add_to_class('_admin', admin_obj)

            admins.append(admin_obj)

        return admins

    def unregister(self, model_or_iterable):
        """
        Unregisters the given model(s).

        If a model isn't already registered, this will raise NotRegistered.
        """
        if isinstance(model_or_iterable, ModelBase):
            model_or_iterable = [model_or_iterable]
        for model in model_or_iterable:
            if model not in self._registry:
                raise NotRegistered('The model %s is not registered' % (
                    model.__name__,
                ))
            del self._registry[model]

    def has_permission(self, request):
        """
        Returns True if the given HttpRequest has permission to view
        *at least one* page in the admin site.
        """
        return request.user.is_active and request.user.is_staff

    def admin_view(self, view, cacheable=False):
        """
        Decorator to create an admin view attached to this ``AdminSite``. This
        wraps the view and provides permission checking by calling
        ``self.has_permission``.

        You'll want to use this from within ``AdminSite.get_urls()``:

            class MyAdminSite(AdminSite):

                def get_urls(self):
                    from django.conf.urls import patterns, url

                    urls = super(MyAdminSite, self).get_urls()
                    urls += patterns('',
                        url(r'^my_view/$', self.admin_view(some_view))
                    )
                    return urls

        By default, admin_views are marked non-cacheable using the
        ``never_cache`` decorator. If the view can be safely cached, set
        cacheable=True.
        """
        def inner(request, *args, **kwargs):
            if not self.has_permission(request):
                if request.path == reverse('account_logout'):
                    index_path = reverse('index', current_app=self.name)
                    return HttpResponseRedirect(index_path)
                return self.login(request)
            return view(request, *args, **kwargs)
        if not cacheable:
            inner = never_cache(inner)
        # We add csrf_protect here so this function can be used as a utility
        # function for any view, without having to repeat 'csrf_protect'.
        if not getattr(view, 'csrf_exempt', False):
            inner = csrf_protect(inner)
        return update_wrapper(inner, view)

    def get_urls(self):

        def wrap(view, cacheable=False):
            def wrapper(*args, **kwargs):
                return self.admin_view(view, cacheable)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        # Admin-site-wide views.
        urlpatterns = [
            url(r'^$',
                wrap(self.adminInterface),
                name='index'),
            url(r'^middleware_token/$',
                wrap(self.middleware_token),
                name='freeadmin_middleware_token'),
            url(r'^help/$',
                wrap(self.help),
                name="freeadmin_help"),
            url(r'^menu\.json/$',
                wrap(self.menu),
                name="freeadmin_menu"),
            url(r'^alert/status/$',
                wrap(self.alert_status),
                name="freeadmin_alert_status"),
            url(r'^alert/$',
                wrap(self.alert_detail),
                name="freeadmin_alert_detail"),
        ]

        # Add in each model's views.
        for model_admin in self._registry.values():
            urlpatterns += [
                url(r'^%s/%s/' % (
                    model_admin.app_label,
                    model_admin.module_name,
                ), include(model_admin.urls)),
            ]

        return urlpatterns

    @property
    def urls(self):
        return self.get_urls()

    @never_cache
    def adminInterface(self, request):
        from freenasUI.network.models import GlobalConfiguration
        from freenasUI.system.models import Advanced, Settings

        view = appPool.hook_app_index('freeadmin', request)
        view = [_f for _f in view if _f]
        if view:
            return view[0]

        try:
            console = Advanced.objects.all().order_by('-id')[0].adv_consolemsg
        except:
            console = False
        try:
            hostname = GlobalConfiguration.objects.order_by(
                '-id')[0].get_hostname()
        except:
            hostname = None

        try:
            settings = Settings.objects.all().order_by('-id')[0]
            wizard = not settings.stg_wizardshown
            if settings.stg_wizardshown is False:
                settings.stg_wizardshown = True
                settings.save()
        except:
            wizard = False
        sw_version = get_sw_version()
        sw_version_footer = get_sw_version(strip_build_num=True).split('-', 1)[-1]

        return render(request, 'freeadmin/index.html', {
            'consolemsg': console,
            'hostname': hostname,
            'sw_name': get_sw_name(),
            'sw_year': get_sw_year(),
            'sw_version': sw_version,
            'sw_version_footer': sw_version_footer,
            'cache_hash': hashlib.md5(sw_version.encode('utf8')).hexdigest(),
            'css_hook': appPool.get_base_css(request),
            'js_hook': appPool.get_base_js(request),
            'menu_hook': appPool.get_top_menu(request),
            'wizard': wizard,
        })

    @never_cache
    def middleware_token(self, request):
        from freenasUI.middleware.client import client
        with client as c:
            middleware_token = c.call('auth.generate_token', timeout=10)
        return HttpResponse(json.dumps({
            'token': middleware_token,
        }), content_type="application/json")

    @never_cache
    def help(self, request):
        return render(request, 'freeadmin/help.html', {})

    @never_cache
    def menu(self, request):
        from freenasUI.freeadmin.navtree import navtree
        try:
            navtree.generate(request)
            final = navtree.dijitTree(request.user)
            data = json.dumps(final)
        except Exception as e:
            log.debug(
                "Fatal error while generating the tree json: %s",
                e,
                exc_info=True,
            )
            data = ""

        return HttpResponse(data, content_type="application/json")

    @never_cache
    def alert_status(self, request):
        from freenasUI.system.models import Alert
        from freenasUI.system.alert import alert_node, alertPlugins
        dismisseds = [a.message_id for a in Alert.objects.filter(node=alert_node())]
        alerts = alertPlugins.get_alerts()
        current = 'OK'
        for alert in alerts:
            # Skip dismissed alerts
            if alert.getId() in dismisseds:
                continue
            status = alert.getLevel()
            if (
                (status == 'WARN' and current == 'OK') or
                status == 'CRIT' and
                current in ('OK', 'WARN')
            ):
                current = status
        return HttpResponse(current)

    @never_cache
    def alert_detail(self, request):
        from freenasUI.system.models import Alert
        from freenasUI.system.alert import alert_node, alertPlugins
        dismisseds = [a.message_id for a in Alert.objects.filter(node=alert_node())]
        alerts = alertPlugins.get_alerts()
        return render(request, "freeadmin/alert_status.html", {
            'alerts': alerts,
            'dismisseds': dismisseds,
        })


site = FreeAdminSite()
