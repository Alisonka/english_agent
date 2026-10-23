"""The three sources, in the order the agent must try them."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(
    frozen=True
)  # frozen=True делает объект неизменяемым (immutable): после создания его поля нельзя переприсвоить.
class Site:
    name: str  # englex
    domain: str  # englex.ru
    sitemaps: tuple[str, ...]  # кортеж URL-адресов карт сайтов (sitemaps)
    # Only follow child sitemaps whose URL contains this substring (for sitemap indexes).
    child_sitemap_filter: str | None  # фильтр для вложенных карт сайта
    # Only index article URLs whose path starts with one of these prefixes.
    article_path_prefixes: tuple[str, ...]

    def owns(self, url: str) -> bool:  # проверяет, принадлежит ли URL-адрес этому сайту
        host = (urlparse(url).hostname or "").lower()
        return host == self.domain or host.endswith("." + self.domain)

    def is_article(self, url: str) -> bool:  # проверяет, является ли URL-адрес статьёй
        path = urlparse(url).path
        return self.owns(url) and any(path.startswith(prefix) for prefix in self.article_path_prefixes)


ENGLEX = Site(
    name="englex",
    domain="englex.ru",
    sitemaps=("https://englex.ru/sitemap.xml",),
    child_sitemap_filter="sitemap-pt-post-",
    article_path_prefixes=("/",),
)

SKYENG = Site(
    name="skyeng",
    domain="skyeng.ru",
    sitemaps=("https://skyeng.ru/articles/sitemap.xml", "https://skyeng.ru/magazine/sitemap.xml"),
    child_sitemap_filter=None,
    article_path_prefixes=("/articles/", "/magazine/"),
)

SKYSMART = Site(
    name="skysmart",
    domain="skysmart.ru",
    sitemaps=("https://skysmart.ru/assets/sitemaps/skysmart_articles_sitemap.xml",),
    child_sitemap_filter=None,
    article_path_prefixes=("/articles/english/",),
)

SITES: tuple[Site, ...] = (ENGLEX, SKYENG, SKYSMART)
SITE_BY_NAME: dict[str, Site] = {site.name: site for site in SITES}
SITE_ORDER: tuple[str, ...] = tuple(site.name for site in SITES)


def site_for_url(url: str) -> Site | None:  # По произвольному URL находит, какому сайту он принадлежит
    return next((site for site in SITES if site.owns(url)), None)
