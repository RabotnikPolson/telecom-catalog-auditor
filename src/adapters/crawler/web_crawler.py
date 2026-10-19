import asyncio
from dataclasses import dataclass
from typing import Any
from playwright.async_api import Browser, Playwright, async_playwright


@dataclass
class CrawlResult:
    url: str
    markdown: str
    title: str = ""
    status_code: int = 200
    success: bool = True
    error: str | None = None
    images_count: int = 0


class WebCrawler:

    def __init__(
        self,
        max_concurrent: int = 2,
        headless: bool = True,
        timeout_ms: int = 30000,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> "WebCrawler":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    async def start(self) -> None:
        if not self._playwright:
            self._playwright = await async_playwright().start()
        if not self._browser:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                ],
            )

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def crawl(self, url: str) -> CrawlResult:
        if not self._browser:
            await self.start()

        async with self.semaphore:
            assert self._browser is not None
            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="ru-RU",
                timezone_id="Asia/Almaty",
                extra_http_headers={
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                window.chrome = {
                    runtime: {}
                };
            """)

            # Route filter: abort images, fonts, media, and heavy analytics trackers
            async def _route_filter(route: Any) -> None:
                try:
                    req = route.request
                    res_type = req.resource_type
                    req_url_lower = req.url.lower()

                    if res_type in ("image", "media", "font"):
                        await route.abort()
                        return

                    if any(
                        ext in req_url_lower
                        for ext in (
                            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
                            ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3",
                        )
                    ):
                        await route.abort()
                        return

                    if any(
                        tracker in req_url_lower
                        for tracker in (
                            "google-analytics.com",
                            "googletagmanager.com",
                            "mc.yandex.ru",
                            "yandex.ru/metrika",
                            "hotjar.com",
                            "connect.facebook.net",
                        )
                    ):
                        await route.abort()
                        return

                    await route.continue_()
                except Exception:
                    pass

            await context.route("**/*", _route_filter)

            page = await context.new_page()
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                status_code = response.status if response else 200

                # Fast selector wait: wait for content/specs or gallery rather than hanging 3s on networkidle
                try:
                    await page.wait_for_selector(
                        "table, dl, .specifications, .item__slider-slide, .item__slider-thumb, .gallery, [data-gallery]",
                        timeout=1800,
                    )
                except Exception:
                    pass

                # Brief debounce for JavaScript hydration
                await page.wait_for_timeout(300)

                # Fast accordion / spoiler expansion via 2 combined locators (tabs first, then expand buttons)
                tab_sel = "[data-tab='specifications'], [data-tab='specs'], [role='tab']:has-text('Характеристики')"
                try:
                    tab_loc = page.locator(tab_sel).first
                    if await tab_loc.is_visible():
                        await tab_loc.click(timeout=600)
                        await page.wait_for_timeout(150)
                except Exception:
                    pass

                expand_sel = (
                    "button:has-text('Все характеристики'), "
                    "button:has-text('все характеристики'), "
                    "button:has-text('Показать все'), "
                    "a:has-text('Все характеристики'), "
                    "a:has-text('Показать все'), "
                    "button:has-text('Характеристики'), "
                    "a:has-text('Характеристики'), "
                    "button:has-text('Подробнее'), "
                    "a:has-text('Подробнее')"
                )
                try:
                    expand_loc = page.locator(expand_sel).first
                    if await expand_loc.is_visible():
                        await expand_loc.click(timeout=600)
                        await page.wait_for_timeout(150)
                except Exception:
                    pass

                extracted = await page.evaluate(
                    """() => {
                        const noiseSelectors = [
                            'script', 'style', 'noscript', 'svg', 'iframe',
                            'header', 'footer', 'nav', 'form', 'aside',
                            '.cookie', '.cookies', '.banner', '.reviews', '.comments',
                            '.popup', '.modal', '.breadcrumbs', '.similar-products',
                            '.recommended', '.pagination', '.catalog-menu'
                        ];
                        noiseSelectors.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => el.remove());
                        });

                        const title = document.querySelector('h1')?.innerText?.trim() || document.title || '';

                        function clean(t) {
                            return (t || '').replace(/\\s+/g, ' ').trim();
                        }

                        let lines = [];
                        if (title) {
                            lines.push('# ' + title);
                            lines.push('');
                        }

                        let foundStructuredSpecs = false;

                        const tables = document.querySelectorAll('table');
                        if (tables.length > 0) {
                            lines.push('## Specifications (Tables)');
                            tables.forEach(table => {
                                const rows = table.querySelectorAll('tr');
                                rows.forEach(row => {
                                    const cells = Array.from(row.querySelectorAll('th, td')).map(c => clean(c.innerText));
                                    if (cells.length >= 2 && cells[0] && cells[1]) {
                                        lines.push(`* ${cells[0]}: ${cells.slice(1).join(' - ')}`);
                                        foundStructuredSpecs = true;
                                    }
                                });
                            });
                            lines.push('');
                        }

                        const dls = document.querySelectorAll('dl');
                        if (dls.length > 0) {
                            lines.push('## Specifications (Lists)');
                            dls.forEach(dl => {
                                const dts = dl.querySelectorAll('dt');
                                const dds = dl.querySelectorAll('dd');
                                for (let i = 0; i < Math.min(dts.length, dds.length); i++) {
                                    const k = clean(dts[i].innerText);
                                    const v = clean(dds[i].innerText);
                                    if (k && v) {
                                        lines.push(`* ${k}: ${v}`);
                                        foundStructuredSpecs = true;
                                    }
                                }
                            });
                            lines.push('');
                        }

                        const descSelectors = [
                            '[itemprop="description"]', '.product-description',
                            '.description', '#tab-description', '.detail-text',
                            '.product-about', '.specifications'
                        ];
                        let descText = '';
                        for (const sel of descSelectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText.trim().length > 30) {
                                descText += clean(el.innerText) + '\\n';
                            }
                        }

                        if (descText) {
                            lines.push('## Description');
                            lines.push(descText.substring(0, 4000));
                            lines.push('');
                        }

                        if (!foundStructuredSpecs) {
                            const bodyText = document.body ? document.body.innerText : '';
                            const cleanedBody = bodyText
                                .split('\\n')
                                .map(l => clean(l))
                                .filter(l => l.length > 3 && !l.includes('Корзина') && !l.includes('Контакты'))
                                .join('\\n');

                            lines.push('## Page Content');
                            lines.push(cleanedBody.substring(0, 5000));
                        }

                        let imagesCount = 0;
                        const kaspiSlides = document.querySelectorAll('.item__slider-slide, .gallery__slider-item');
                        if (kaspiSlides.length > 0) {
                            imagesCount = kaspiSlides.length;
                        } else {
                            const kaspiThumbs = document.querySelectorAll('.item__slider-thumb, .gallery__thumb');
                            if (kaspiThumbs.length > 0) {
                                imagesCount = kaspiThumbs.length;
                            } else {
                                const galleryItems = document.querySelectorAll(
                                    '.product-images-slider__item, .product-card__gallery img, .swiper-slide img, [data-gallery] img, .gallery img, .product-gallery img, .fotorama img'
                                );
                                if (galleryItems.length > 0) {
                                    imagesCount = galleryItems.length;
                                } else {
                                    const prodImgs = document.querySelectorAll('img[itemprop="image"], .main-image img, #main-image, img[src*="cdn-kaspi.kz/img/m/p/"]');
                                    imagesCount = prodImgs.length > 0 ? prodImgs.length : (document.querySelectorAll('img').length > 0 ? 1 : 0);
                                }
                            }
                        }

                        return {
                            title: title,
                            markdown: lines.join('\\n').trim(),
                            imagesCount: imagesCount
                        };
                    }"""
                )

                title = extracted.get("title", "")
                markdown = extracted.get("markdown", "")
                ref_images_cnt = int(extracted.get("imagesCount", 0))

                md_lower = markdown.lower()
                is_cf = (
                    "checking if the site connection is secure" in md_lower
                    or "verify you are human" in md_lower
                    or "checking your browser" in md_lower
                    or "cloudflare turnstile" in md_lower
                    or "just a moment..." in md_lower
                    or "attention required! | cloudflare" in md_lower
                )
                if is_cf:
                    return CrawlResult(
                        url=url,
                        title=title,
                        markdown=markdown,
                        status_code=403,
                        success=False,
                        error="Cloudflare anti-bot verification challenge page",
                        images_count=ref_images_cnt,
                    )

                is_error_page = (
                    status_code >= 400
                    or "страница не найдена" in md_lower
                    or "404 not found" in md_lower
                    or "товар не найден" in md_lower
                )
                if is_error_page:
                    return CrawlResult(
                        url=url,
                        title=title,
                        markdown=markdown,
                        status_code=status_code,
                        success=False,
                        error=f"HTTP {status_code} Page Not Found" if status_code >= 400 else "Content indicates Page Not Found",
                        images_count=ref_images_cnt,
                    )

                return CrawlResult(
                    url=url,
                    title=title,
                    markdown=markdown,
                    status_code=status_code,
                    success=True,
                    error=None,
                    images_count=ref_images_cnt,
                )
            except Exception as e:
                return CrawlResult(
                    url=url,
                    title="",
                    markdown="",
                    status_code=0,
                    success=False,
                    error=str(e),
                )
            finally:
                await page.close()
                await context.close()
