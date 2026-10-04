from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve
from chat.sitemaps import StaticViewSitemap
from django.contrib.sitemaps.views import sitemap


sitemaps = {
    'static': StaticViewSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('chat.urls')),
    path('googled8b50b52752f6cc3.html', serve, {'document_root': settings.BASE_DIR, 'path': 'googled8b50b52752f6cc3.html'}),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='django.contrib.sitemaps.views.sitemap'),

]

# The modern way to serve static and media files during development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    # If you have media files (profile pictures, etc.)
    # urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)