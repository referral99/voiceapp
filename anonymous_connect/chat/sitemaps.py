from django.contrib.sitemaps import Sitemap
from django.urls import reverse

class StaticViewSitemap(Sitemap):
    priority = 0.8
    changefreq = 'daily'

    def items(self):
        # Public pages to include in the sitemap. These names must match
        # the `name=` values defined in chat/urls.py.
        return [
            'home',
            'privacy_policy',
            'terms_conditions',
            'refund_policy',
            'contact_us',
            'service_fulfillment',
        ]

    def location(self, item):
        return reverse(item)
