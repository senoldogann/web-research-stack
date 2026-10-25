"""The page read again in Camoufox: what the guard holds there, how long it may take, and what it may fetch.

1.4.2 added a second engine to ``HeadlessBrowserManager``: when the headless Chrome is turned away by a site's bot
check (a captcha, an access wall, or a timeout) the page is read once more in Camoufox, a Firefox made to look like a
person's browser. It was installed from the first page that needed it: Camoufox downloads its browser (from GitHub),
the model its fingerprints are drawn from (from GitHub) and its ad blocker (the latest, from addons.mozilla.org) the
moment it starts, in the middle of the read, which waits for it with nothing to bound the wait. And the render of 1.4.1
that hung on a page that blocks the browser, and the guard that held only the first engine's page, were not on this
path either.

Now Camoufox starts only from what is on the machine, so that no read waits for a download; what it lacks is fetched
once, in the background, by ``python -m camoufox fetch`` (or by you), and the page that asked is returned as the first
engine read it. Its start is bounded, its contexts are held as the first engine's are (the requests of a page and its
frames and a window it opens; not its web sockets, a worker's requests or a redirect, which Playwright cannot hold in
Firefox: ``tests/test_web_browser_camoufox.py`` measures that), its render and the escalation are bounded, a page is
returned only if it ended at an address the guard allows, and ``WEB_TOOLS_BROWSER_STEALTH=false`` turns it off. The
fakes below stand for Playwright and for the camoufox package; a real Camoufox is in the other file.
"""

from __future__ import annotations

import asyncio
import builtins
import enum
import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest
from rebinding_support import PUBLIC_A, World, html
from test_web_browser_hang import Chrome, Context, HangingPage, WholePage

from web_scraper.mcp_server import McpServer
from web_scraper.web_tools import browser as browser_module
from web_scraper.web_tools.browser import (
    BrowserRenderError,
    BrowserUnavailableError,
    HeadlessBrowserManager,
    camoufox_missing,
)
from web_scraper.web_tools.fetcher import SafeFetcher
from web_scraper.web_tools.service import WebToolsService
from web_scraper.web_tools.settings import WebToolsSettings

HANG = 10.0
LONG = "A page with something to say, and enough of it to be one. " * 5
CHALLENGE = '<html><body><div class="cf-turnstile">Verify you are human</div></body></html>'


class ChallengePage(WholePage):
    """What the site's bot check gives the first engine: a 403 and a widget, and nothing to read."""

    async def goto(self, url: str, wait_until: str, timeout: int) -> Any:
        return types.SimpleNamespace(status=403)

    async def evaluate(self, expression: str, limit: int = 200) -> Any:
        return {"chars": 20, "head": "Verify you are human"}

    async def content(self) -> str:
        return f"<html><head><title>Just a moment</title></head><body>{CHALLENGE}</body></html>"


class RecordingContext(Context):
    """A context that writes down, in order, what is done to it."""

    def __init__(self, page: Any, **options: Any) -> None:
        super().__init__(page, **options)
        self.events: list[str] = []

    async def route(self, pattern: str, handler: Any) -> None:
        self.events.append("route")

    async def route_web_socket(self, pattern: str, handler: Any) -> None:
        self.events.append("route_web_socket")

    def on(self, event: str, handler: Any) -> None:
        self.events.append(f"on:{event}")

    async def new_page(self) -> Any:
        self.events.append("new_page")
        return self.page


class Firefox(Chrome):
    """Camoufox's browser as Playwright hands it."""


class Installed:
    """The camoufox package as the manager sees it: what it is asked, and what it starts."""

    def __init__(
        self, tmp_path: Path, *, browser: bool = True, addon: bool = True, model: bool = True
    ):
        self.tmp_path = tmp_path
        self.browser, self.addon, self.model = browser, addon, model
        self.path_asked: list[bool] = []
        self.started: list[dict[str, Any]] = []
        self.exited = 0
        self.firefox: Firefox = Firefox(Context(WholePage()))
        self.hang_on_enter = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        installed = self

        class DefaultAddons(enum.Enum):
            UBO = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"

        def camoufox_path(download_if_missing: bool = True) -> Path:
            installed.path_asked.append(download_if_missing)
            if not installed.browser:
                raise FileNotFoundError("Camoufox is not installed")
            return installed.tmp_path

        def get_addon_path(name: str) -> str:
            folder = installed.tmp_path / "addons" / name
            if installed.addon:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "manifest.json").write_text("{}")
            return str(folder)

        class AsyncCamoufox:
            def __init__(self, **options: Any) -> None:
                installed.started.append(options)

            async def __aenter__(self) -> Firefox:
                if installed.hang_on_enter:
                    await asyncio.Event().wait()
                return installed.firefox

            async def __aexit__(self, *args: Any) -> None:
                installed.exited += 1

        modules = {
            "camoufox": types.ModuleType("camoufox"),
            "camoufox.addons": types.SimpleNamespace(
                DefaultAddons=DefaultAddons, get_addon_path=get_addon_path
            ),
            "camoufox.fpgen_model": types.SimpleNamespace(is_pinned=lambda: installed.model),
            "camoufox.pkgman": types.SimpleNamespace(camoufox_path=camoufox_path),
            "camoufox.async_api": types.SimpleNamespace(AsyncCamoufox=AsyncCamoufox),
        }
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture(autouse=True)
def _short_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_module, "browser_request_allowed", lambda url: True)
    monkeypatch.setattr(browser_module, "_HARD_LIMIT_MARGIN_SECONDS", 0.2)
    monkeypatch.setattr(browser_module, "_CLOSE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(browser_module, "_STOP_TIMEOUT_SECONDS", 0.2)


@pytest.fixture
def camoufox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Installed:
    installed = Installed(tmp_path)
    installed.install(monkeypatch)
    return installed


def _manager(primary: Chrome, **settings: Any) -> HeadlessBrowserManager:
    manager = HeadlessBrowserManager(post_load_wait_ms=0, settle_stable_ms=0, **settings)
    manager.timeout_seconds = 0.4  # the constructor keeps it to a second or more
    manager._browser = primary
    manager._engine = "chromium"
    return manager


class TestCamoufoxIsStartedOnlyFromWhatIsInstalled:
    def test_it_is_ready_when_its_browser_its_ad_blocker_and_its_model_are_there(
        self, camoufox: Installed
    ) -> None:
        assert camoufox_missing() == ""
        assert camoufox.path_asked == [
            False
        ]  # asked whether the browser is there, never to fetch it

    @pytest.mark.parametrize(
        ("lacks", "says"),
        [
            ({"browser": False}, "browser is not installed"),
            ({"addon": False}, "ad blocker (UBO) is not installed"),
            ({"model": False}, "fingerprint model is not installed"),
        ],
    )
    def test_what_it_lacks_is_named_with_the_command_that_fetches_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lacks: dict[str, bool], says: str
    ) -> None:
        Installed(tmp_path, **lacks).install(monkeypatch)

        reason = camoufox_missing()

        assert says in reason and "python -m camoufox fetch" in reason

    def test_without_the_package_it_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "camoufox", None)  # an import of it fails

        assert camoufox_missing() == "the package camoufox is not installed"

    def test_a_camoufox_that_cannot_be_imported_for_another_reason_is_not_there_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # camoufox is a dependency of this package, so it is imported on every server: a machine that lacks
        # something it needs (a system library) must not take the page reads down with an OSError.
        real_import = builtins.__import__

        def refusing(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "camoufox" or name.startswith("camoufox."):
                raise OSError("a system library is missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.delitem(sys.modules, "camoufox", raising=False)
        monkeypatch.setattr(builtins, "__import__", refusing)

        assert "cannot be imported here (OSError)" in camoufox_missing()
        assert browser_module.camoufox_installed() is False
        assert HeadlessBrowserManager().has_stealth is False

    def test_a_camoufox_too_old_to_be_asked_is_not_started(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        Installed(tmp_path).install(monkeypatch)
        monkeypatch.setitem(sys.modules, "camoufox.fpgen_model", None)  # the module 0.5.8 added

        assert "0.5.8 or later" in camoufox_missing()

    @pytest.mark.asyncio
    async def test_a_page_that_needs_it_when_it_is_not_installed_is_read_as_the_first_engine_read_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        manager = _manager(Chrome(Context(ChallengePage())))

        page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        # The challenge page the first engine got is what the caller gets (the service says so); nothing was
        # started, so nothing was downloaded.
        assert page.status_code == 403 and "Verify you are human" in page.html
        assert installed.started == [] and installed.path_asked == [False]
        assert "python -m camoufox fetch" in manager.snapshot()["stealth_reason"]

    @pytest.mark.asyncio
    async def test_it_is_started_with_the_options_that_keep_the_browsers_own_calls_off(
        self, camoufox: Installed
    ) -> None:
        manager = _manager(Chrome(Context(ChallengePage())))

        page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        (options,) = camoufox.started
        assert page.engine == "camoufox" and page.status_code == 200
        assert options["headless"] is True and options["geoip"] is False
        prefs = options["firefox_user_prefs"]
        assert prefs["app.update.enabled"] is False and prefs["media.gmp-provider.enabled"] is False
        assert prefs["extensions.update.enabled"] is False
        prefs["app.update.enabled"] = True  # a copy: the module's own table is not the browser's
        assert browser_module._STEALTH_OPTIONS["firefox_user_prefs"]["app.update.enabled"] is False

    @pytest.mark.asyncio
    async def test_a_start_that_takes_too_long_ends_and_the_page_the_first_engine_read_is_returned(
        self, camoufox: Installed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(browser_module, "_STEALTH_START_TIMEOUT_SECONDS", 0.2)
        camoufox.hang_on_enter = True
        manager = _manager(Chrome(Context(ChallengePage())))
        began = time.monotonic()

        page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert page.status_code == 403 and time.monotonic() - began < 3
        assert camoufox.exited == 1  # what was started is shut again
        assert "did not start in time" in manager.snapshot()["stealth_reason"]


class TestCamoufoxChosenAsTheBrowser:
    @pytest.mark.asyncio
    async def test_when_it_is_not_fetched_the_log_says_what_to_run_and_nothing_is_downloaded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        manager = HeadlessBrowserManager(preference="camoufox")

        with caplog.at_level("WARNING"), pytest.raises(BrowserUnavailableError):
            await manager.start()

        assert "python -m camoufox fetch" in caplog.text
        assert installed.started == [] and installed.path_asked == [False]


class TestItCanBeTurnedOff:
    @pytest.mark.asyncio
    async def test_a_manager_that_may_not_does_not_read_a_page_again(
        self, camoufox: Installed
    ) -> None:
        manager = _manager(Chrome(Context(ChallengePage())), stealth_enabled=False)

        page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert manager.has_stealth is False and page.status_code == 403
        assert camoufox.started == []
        with pytest.raises(BrowserUnavailableError, match="turned off"):
            await manager.render_stealth("https://page.test/")

    def test_the_setting_is_on_by_default_and_reaches_the_manager(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert WebToolsSettings().browser_stealth is True
        assert WebToolsService.from_env().browser.stealth_enabled is True  # type: ignore[union-attr]

        monkeypatch.setenv("WEB_TOOLS_BROWSER_STEALTH", "false")
        service = WebToolsService.from_env()

        assert service.browser is not None and service.browser.stealth_enabled is False

    def test_fetching_what_it_lacks_is_on_by_default_in_the_server_and_can_be_turned_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert WebToolsSettings().browser_stealth_fetch is True
        assert WebToolsService.from_env().browser.stealth_fetch is True  # type: ignore[union-attr]

        monkeypatch.setenv("WEB_TOOLS_BROWSER_STEALTH_FETCH", "false")
        service = WebToolsService.from_env()

        assert service.browser is not None and service.browser.stealth_fetch is False

    def test_camoufox_is_a_dependency_for_the_pythons_that_can_run_it_and_not_an_extra(
        self,
    ) -> None:
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        dependencies = pyproject.split("\ndependencies = [", 1)[1].split("\n]", 1)[0]

        # 0.5.8 is the release whose browser and fingerprint model the package names and checks; Camoufox needs
        # Python 3.10, and this package still installs on 3.9 (the page is then read by Chrome alone). Not on an
        # Intel Mac (1.4.6), where indexed-zstd, which it needs, has no wheel (tests/test_packaging.py holds the marker).
        assert (
            "\"camoufox>=0.5.8; python_version >= '3.10'"
            " and (sys_platform != 'darwin' or platform_machine != 'x86_64')\""
        ) in dependencies
        assert "\nstealth = [" not in pyproject  # the extra of 1.4.2's plan is gone
        assert (
            "geoip" not in dependencies
        )  # its GeoIP database (115 MB) is for a look-up that is off


class FakeFetch:
    """``python -m camoufox fetch`` without running it: it takes as long as it is let to, and ends as told."""

    def __init__(self, installed: Installed) -> None:
        self.installed = installed
        self.calls = 0
        self.release = asyncio.Event()
        self.result = (True, "Camoufox installed")
        self.puts_it_there = True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetch = self

        async def run(self: browser_module.CamoufoxFetch) -> tuple[bool, str]:
            fetch.calls += 1
            await fetch.release.wait()
            if fetch.result[0] and fetch.puts_it_there:
                fetch.installed.browser = True
            return fetch.result

        monkeypatch.setattr(browser_module.CamoufoxFetch, "run_command", run)


async def _finished(manager: HeadlessBrowserManager) -> None:
    task = browser_module.camoufox_fetch.task
    if task is not None:
        await asyncio.wait_for(task, HANG)


class TestWhatCamoufoxLacksIsFetchedApartFromTheRead:
    @pytest.mark.asyncio
    async def test_the_first_page_that_needs_it_starts_one_fetch_and_is_returned_as_the_first_engine_read_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        fetch = FakeFetch(installed)
        fetch.install(monkeypatch)
        manager = _manager(Chrome(Context(ChallengePage())), stealth_fetch=True)

        try:
            first = await asyncio.wait_for(manager.render("https://page.test/"), HANG)
            second = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

            # Neither read waited for the fetch, which has not ended; the second did not start another.
            assert first.status_code == 403 and second.status_code == 403
            assert fetch.calls == 1 and installed.started == []
            snapshot = manager.snapshot()
            assert snapshot["stealth_fetching"] is True and snapshot["stealth_fetch"] is True
            assert "fetching it in the background" in snapshot["stealth_reason"]
        finally:
            fetch.release.set()
            await _finished(manager)

    @pytest.mark.asyncio
    async def test_when_the_fetch_has_put_it_there_the_next_page_is_read_in_camoufox(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        fetch = FakeFetch(installed)
        fetch.install(monkeypatch)
        manager = _manager(Chrome(Context(ChallengePage())), stealth_fetch=True)

        with caplog.at_level("INFO", logger=browser_module.logger.name):
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)
            fetch.release.set()
            await _finished(manager)
            page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert page.engine == "camoufox" and page.status_code == 200
        assert fetch.calls == 1 and manager.snapshot()["stealth_fetching"] is False
        assert "running `python -m camoufox fetch` once, in the background" in caplog.text
        assert "about 100 MB to download and 2.3 GB on disk" in caplog.text
        assert "Camoufox fetched" in caplog.text

    @pytest.mark.asyncio
    async def test_a_fetch_that_failed_is_said_and_not_tried_again_for_a_while(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        fetch = FakeFetch(installed)
        fetch.result = (False, "Error: Release not found (404)")
        fetch.install(monkeypatch)
        manager = _manager(Chrome(Context(ChallengePage())), stealth_fetch=True)

        with caplog.at_level("INFO", logger=browser_module.logger.name):
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)
            fetch.release.set()
            await _finished(manager)
            page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

            assert page.status_code == 403 and fetch.calls == 1  # not again, so soon
            monkeypatch.setattr(browser_module, "_STEALTH_FETCH_RETRY_SECONDS", 0.0)
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)
            await _finished(manager)

        assert fetch.calls == 2  # when the wait is over it is tried again
        assert "Fetching Camoufox failed (Error: Release not found (404))" in caplog.text
        assert "run `python -m camoufox fetch` yourself" in caplog.text

    @pytest.mark.asyncio
    async def test_a_fetch_that_ends_well_but_leaves_something_out_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        fetch = FakeFetch(installed)
        fetch.puts_it_there = False
        fetch.install(monkeypatch)
        manager = _manager(Chrome(Context(ChallengePage())), stealth_fetch=True)

        with caplog.at_level("INFO", logger=browser_module.logger.name):
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)
            fetch.release.set()
            await _finished(manager)

        assert "it finished, but its browser is not installed" in caplog.text
        assert browser_module.camoufox_fetch.failed_at is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["may_not", "no_package", "too_old"])
    async def test_nothing_is_fetched_when_it_may_not_be_or_when_fetching_would_not_help(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
    ) -> None:
        installed = Installed(tmp_path, browser=False)
        installed.install(monkeypatch)
        fetch = FakeFetch(installed)
        fetch.install(monkeypatch)
        if case == "no_package":
            monkeypatch.setitem(sys.modules, "camoufox", None)
        if case == "too_old":
            monkeypatch.setitem(sys.modules, "camoufox.fpgen_model", None)
        manager = _manager(Chrome(Context(ChallengePage())), stealth_fetch=case != "may_not")

        page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert page.status_code == 403
        assert fetch.calls == 0 and browser_module.camoufox_fetch.task is None
        assert manager.snapshot()["stealth_fetching"] is False

    @pytest.mark.asyncio
    async def test_a_manager_made_by_hand_does_not_fetch_unless_it_is_told_to(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        Installed(tmp_path, browser=False).install(monkeypatch)

        assert HeadlessBrowserManager().stealth_fetch is False

    @pytest.mark.asyncio
    async def test_the_command_is_run_and_the_last_line_it_printed_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def command(code: str) -> None:
            monkeypatch.setattr(
                browser_module, "_STEALTH_FETCH_COMMAND", (sys.executable, "-c", code)
            )

        fetch = browser_module.CamoufoxFetch()

        command("print('Syncing repositories...'); print('Camoufox installed')")
        assert await fetch.run_command() == (True, "Camoufox installed")
        command("import sys; print('Error: no space left', file=sys.stderr); sys.exit(3)")
        assert await fetch.run_command() == (False, "Error: no space left")
        command("import sys; sys.exit(4)")
        assert await fetch.run_command() == (False, "exit status 4")

    @pytest.mark.asyncio
    async def test_a_command_that_does_not_end_is_stopped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pidfile = tmp_path / "pid"
        script = (
            "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "print('fetching', flush=True)\ntime.sleep(60)"
        )
        monkeypatch.setattr(
            browser_module, "_STEALTH_FETCH_COMMAND", (sys.executable, "-c", script, str(pidfile))
        )
        monkeypatch.setattr(browser_module, "_STEALTH_FETCH_TIMEOUT_SECONDS", 1.5)

        result = await asyncio.wait_for(browser_module.CamoufoxFetch().run_command(), HANG)

        assert result == (False, "it did not finish in time")
        await _gone(int(pidfile.read_text()))

    @pytest.mark.asyncio
    async def test_a_fetch_that_is_cancelled_takes_its_command_with_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pidfile = tmp_path / "pid"
        script = (
            "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\n"
            "print('fetching', flush=True)\ntime.sleep(60)"
        )
        monkeypatch.setattr(
            browser_module, "_STEALTH_FETCH_COMMAND", (sys.executable, "-c", script, str(pidfile))
        )
        task = asyncio.ensure_future(browser_module.CamoufoxFetch().run_command())
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            await asyncio.sleep(0.05)
        pid = int(pidfile.read_text())

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _gone(pid)


async def _gone(pid: int) -> None:
    """Wait until the process is no longer there."""
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"process {pid} is still running")


class TestTheContextsOfCamoufoxAreHeld:
    @pytest.mark.asyncio
    async def test_a_page_read_in_camoufox_has_its_context_held_before_the_page_is_made(
        self, camoufox: Installed
    ) -> None:
        context = RecordingContext(WholePage())
        camoufox.firefox = Firefox(context)
        manager = _manager(Chrome(Context(ChallengePage())))

        await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        # The requests and the web sockets are routed on the context, then the page is made, then the windows it
        # opens are closed; all of it is what the first engine's context has (the routes of a web socket are
        # not applied by Camoufox: tests/test_web_browser_camoufox.py).
        assert context.events == ["route", "route_web_socket", "new_page", "on:page"]
        assert context.closed == 1

    @pytest.mark.asyncio
    async def test_a_target_the_guard_refuses_is_not_read_again_either(
        self, camoufox: Installed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(Chrome(Context(ChallengePage())))
        monkeypatch.setattr(browser_module, "browser_request_allowed", lambda url: False)

        with pytest.raises(BrowserRenderError) as raised:
            await manager.render("http://127.0.0.1:8080/")
        with pytest.raises(BrowserRenderError) as stealth:
            await manager.render_stealth("http://127.0.0.1:8080/")

        assert raised.value.code == stealth.value.code == "unsafe_target"
        assert camoufox.started == []


class TestTheEscalationIsBounded:
    @pytest.mark.asyncio
    async def test_a_page_that_blocks_both_engines_ends_in_two_limits_and_frees_everything(
        self, camoufox: Installed
    ) -> None:
        camoufox.firefox = Firefox(Context(HangingPage()))
        primary = Context(HangingPage())
        manager = _manager(Chrome(primary), max_contexts=1)
        began = time.monotonic()

        with pytest.raises(BrowserRenderError) as raised:
            await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        # The first engine's limit (0.4 s and the margin) and Camoufox's; then the error of the first.
        assert raised.value.code == "timeout" and time.monotonic() - began < 4
        assert primary.closed == 1 and camoufox.firefox.made[0].closed == 1
        assert manager._active == 0 and not manager._context_gate.locked()

    @pytest.mark.asyncio
    async def test_a_camoufox_context_that_will_not_close_shuts_camoufox_and_leaves_the_first_engine(
        self, camoufox: Installed
    ) -> None:
        stuck = Context(WholePage(), hangs_on_close=True)
        camoufox.firefox = Firefox(stuck)
        primary = Chrome(Context(ChallengePage()))
        manager = _manager(primary)

        page = await asyncio.wait_for(manager.render("https://page.test/"), HANG)

        assert page.engine == "camoufox"
        assert manager._stealth_browser is None and camoufox.exited == 1  # Camoufox is shut down,
        assert manager._browser is primary and primary.shut_down is False  # the first engine is not
        assert manager._active == 0

    @pytest.mark.asyncio
    async def test_more_calls_than_the_server_has_tool_slots_all_end_when_both_engines_are_blocked(
        self, camoufox: Installed, world: World
    ) -> None:
        camoufox.firefox = Firefox(Context(HangingPage()))
        manager = _manager(Chrome(Context(HangingPage())), max_contexts=4)
        world.dns.answers("shell.test", [PUBLIC_A])
        world.internet.serve(
            PUBLIC_A,
            lambda request: html(
                "<html><head><title>Shell</title></head><body><main><p>A few lines of text.</p></main>"
                '<script src="/app.js"></script></body></html>'
            ),
        )
        service = WebToolsService(
            settings=WebToolsSettings(
                search_provider="fake", soft_404_check=False, respect_robots=False
            ),
            fetcher=SafeFetcher(timeout_seconds=5, retry_attempts=0),
            browser=manager,
            search_adapters={},
        )
        server = McpServer(service)
        calls = [
            server._call_tool({"name": "web_scrape", "arguments": {"url": "https://shell.test/"}})
            for _ in range(12)
        ]

        try:
            results = await asyncio.wait_for(asyncio.gather(*calls), 60)
        finally:
            await service.close()

        assert len(results) == 12 and not any(result.get("isError") for result in results)
        assert server._request_slots._value == 8  # every slot is back
