from django.conf import settings


def seo(request):
    """Expose SEO-related values to every template.

    - SITE_URL: canonical base URL (no trailing slash) from settings.
    - CANONICAL_URL: absolute URL of the current page, used for the
      <link rel="canonical"> tag and Open Graph / Twitter share URLs.
    """
    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    return {
        'SITE_URL': site_url,
        'CANONICAL_URL': f"{site_url}{request.path}",
    }
