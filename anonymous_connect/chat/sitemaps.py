from django.contrib.sitemaps import Sitemap
from django.urls import reverse

class StaticViewSitemap(Sitemap):
    priority = 0.8
    changefreq = 'daily'

    def items(self):
        # Yaha apne saare important pages ke naam daalo
        return ['home', 'about', 'notes_list'] 

    def location(self, item):
        return reverse(item)