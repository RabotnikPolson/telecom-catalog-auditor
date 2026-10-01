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
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
            )
            page = await context.new_page()
            try:
                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                status_code = response.status if response else 200

                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass

                await page.wait_for_timeout(800)

                spoiler_selectors = [
                    "button:has-text('Все характеристики')",
                    "button:has-text('все характеристики')",
                    "button:has-text('Характеристики')",
                    "button:has-text('характеристики')",
                    "button:has-text('Подробнее')",
                    "button:has-text('Показать все')",
                    "a:has-text('Все характеристики')",
                    "a:has-text('Характеристики')",
                    "a:has-text('Подробнее')",
                    "[data-tab='specifications']",
                    "[data-tab='specs']",
                    "[role='tab']:has-text('Характеристики')",
                ]
                for selector in spoiler_selectors:
                    try:
                        locator = page.locator(selector).first
                        if await locator.is_visible(timeout=300):
                            await locator.click(timeout=800)
                            await page.wait_for_timeout(400)
                    except Exception:
                        continue

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

                        return {
                            title: title,
                            markdown: lines.join('\\n').trim()
                        };
                    }"""
                )

                title = extracted.get("title", "")
                markdown = extracted.get("markdown", "")

                md_lower = markdown.lower()
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
                    )

                return CrawlResult(
                    url=url,
                    title=title,
                    markdown=markdown,
                    status_code=status_code,
                    success=True,
                    error=None,
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
