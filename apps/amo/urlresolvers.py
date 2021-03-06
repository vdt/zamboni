#-*- coding: utf-8 -*-
import hashlib
import urllib
from threading import local
from urlparse import urlparse, urlsplit, urlunsplit

from django.conf import settings
from django.core import urlresolvers
from django.utils import encoding
from django.utils.translation.trans_real import parse_accept_lang_header

import jinja2
import waffle

import amo

# Get a pointer to Django's reverse because we're going to hijack it after we
# define our own.
django_reverse = urlresolvers.reverse


# Thread-local storage for URL prefixes.  Access with {get,set}_url_prefix.
_local = local()


def set_url_prefix(prefix):
    """Set ``prefix`` for the current thread."""
    _local.prefix = prefix


def get_url_prefix():
    """Get the prefix for the current thread, or None."""
    return getattr(_local, 'prefix', None)


def clean_url_prefixes():
    """Purge prefix cache."""
    if hasattr(_local, 'prefix'):
        delattr(_local, 'prefix')


def get_app_redirect(app):
    """Redirect request to another app."""
    prefixer = get_url_prefix()
    old_app = prefixer.app
    prefixer.app = app.short
    (_, _, url) = prefixer.split_path(prefixer.request.get_full_path())
    new_url = prefixer.fix(url)
    prefixer.app = old_app
    return new_url


def reverse(viewname, urlconf=None, args=None, kwargs=None, prefix=None,
            current_app=None, add_prefix=True):
    """Wraps django's reverse to prepend the correct locale and app."""
    prefixer = get_url_prefix()
    # Blank out the script prefix since we add that in prefixer.fix().
    if prefixer:
        prefix = prefix or '/'
    url = django_reverse(viewname, urlconf, args, kwargs, prefix, current_app)
    if prefixer and add_prefix:
        return prefixer.fix(url)
    else:
        return url

# Replace Django's reverse with our own.
urlresolvers.reverse = reverse


class Prefixer(object):

    def __init__(self, request):
        self.request = request
        split = self.split_path(request.path_info)
        self.locale, self.app, self.shortened_path = split

    def split_path(self, path_):
        """
        Split the requested path into (locale, app, remainder).

        locale and app will be empty strings if they're not found.
        """
        path = path_.lstrip('/')

        # Use partition instead of split since it always returns 3 parts.
        first, _, first_rest = path.partition('/')
        second, _, rest = first_rest.partition('/')

        if first.lower() in settings.LANGUAGES:
            if second in amo.APPS:
                return first, second, rest
            else:
                return first, '', first_rest
        elif first in amo.APPS:
            return '', first, first_rest
        else:
            if second in amo.APPS:
                return '', second, rest
            else:
                return '', '', path

    def get_app(self):
        """
        Return a valid application string using the User Agent to guess.  Falls
        back to settings.DEFAULT_APP.
        """
        ua = self.request.META.get('HTTP_USER_AGENT')
        if ua:
            for app in amo.APP_DETECT:
                if app.matches_user_agent(ua):
                    return app.short

        return settings.DEFAULT_APP

    def get_language(self):
        """
        Return a locale code that we support on the site using the
        user's Accept Language header to determine which is best.  This
        mostly follows the RFCs but read bug 439568 for details.
        """
        if 'lang' in self.request.GET:
            lang = self.request.GET['lang'].lower()
            if lang in settings.LANGUAGE_URL_MAP:
                return settings.LANGUAGE_URL_MAP[lang]

        accept = self.request.META.get('HTTP_ACCEPT_LANGUAGE', '')
        return lang_from_accept_header(accept)

    def fix(self, path):
        # In mkt, don't add the app to the URL.
        with_app = not getattr(settings, 'MARKETPLACE', False)
        path = path.lstrip('/')
        url_parts = [self.request.META['SCRIPT_NAME']]

        if path.partition('/')[0] not in settings.SUPPORTED_NONLOCALES:
            locale = self.locale if self.locale else self.get_language()
            url_parts.append(locale)

        if (with_app
              and path.partition('/')[0] not in settings.SUPPORTED_NONAPPS):
            app = self.app if self.app else self.get_app()
            url_parts.append(app)

        url_parts.append(path)

        return '/'.join(url_parts)


def get_outgoing_url(url):
    """
    Bounce a URL off an outgoing URL redirector, such as outgoing.mozilla.org.
    """
    if not settings.REDIRECT_URL:
        return url

    url_netloc = urlparse(url).netloc

    # No double-escaping, and some domain names are excluded.
    if (url_netloc == urlparse(settings.REDIRECT_URL).netloc
        or url_netloc in settings.REDIRECT_URL_WHITELIST):
        return url

    url = encoding.smart_str(jinja2.utils.Markup(url).unescape())
    hash = hashlib.sha1(settings.REDIRECT_SECRET_KEY + url).hexdigest()
    # Let '&=' through so query params aren't escaped.  We probably shouldn't
    # bother to quote the query part at all.
    return '/'.join([settings.REDIRECT_URL.rstrip('/'), hash,
                     urllib.quote(url, safe='/&=')])


def url_fix(s, charset='utf-8'):
    """Sometimes you get an URL by a user that just isn't a real
    URL because it contains unsafe characters like ' ' and so on.  This
    function can fix some of the problems in a similar way browsers
    handle data entered by the user:

    >>> url_fix(u'http://de.wikipedia.org/wiki/Elf (Begriffsklärung)')
    'http://de.wikipedia.org/wiki/Elf%20%28Begriffskl%C3%A4rung%29'

    :param charset: The target charset for the URL if the url was
                    given as unicode string.

    Lifted from Werkzeug.
    """
    if isinstance(s, unicode):
        s = s.encode(charset, 'ignore')
    scheme, netloc, path, qs, anchor = urlsplit(s)
    path = urllib.quote(path, '/%:')
    qs = urllib.quote_plus(qs, ':&=')
    return urlunsplit((scheme, netloc, path, qs, anchor))


def lang_from_accept_header(header):
    # Map all our lang codes and any prefixes to the locale code.
    langs = [(k.lower(), v) for k, v in settings.LANGUAGE_URL_MAP.items()]
    # Start with prefixes so any real matches override them.
    lang_url_map = dict((k.split('-')[0], v) for k, v in langs)
    lang_url_map.update(langs)

    # If we have a lang or a prefix of the lang, return the locale code.
    for lang, _ in parse_accept_lang_header(header.lower()):
        if lang in lang_url_map:
            return lang_url_map[lang]
        prefix = lang.split('-')[0]
        if prefix in lang_url_map:
            return lang_url_map[prefix]

    return settings.LANGUAGE_CODE
