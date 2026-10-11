from pathlib import Path

from blackice_mcp.server import browser_logfile, console_messages

CONSOLE_READY = (
    '[29472:44332:1010/172731.297:INFO:CONSOLE:10] "[clientDataStore] window.clientData.setData is '
    'ready", source: reui://ui/assets/index-DHzRc2ja.js (10)'
)

# A policy refusal, which is a console message rather than a CEF error
CONSOLE_CSP = (
    "[29472:44332:1010/172731.838:INFO:CONSOLE:0] \"Framing 'https://example.com/' violates the "
    "following Content Security Policy directive: \"frame-ancestors 'self'\". The request has been "
    "blocked."
)

# Chromium's own chatter, which is the bulk of that file
CHROMIUM_NOISE = (
    r"[40112:32812:1010/121854.795:ERROR:components\system_cpu\cpu_probe_win.cc:112] "
    r"PdhAddEnglishCounter failed for '\Hyper-V Hypervisor Logical Processor(_Total)\% Total Run "
    r"Time': Error (0x13D) while retrieving error. (0xC0000BB8)"
)

# Some CEF versions parenthesise the console line number instead of colonising it
CONSOLE_PARENTHESISED = (
    '[1:2:0101/000000.000:ERROR:CONSOLE(42)] "boom", source: https://ugc-frame-x/index.html (42)'
)


def test_browser_log_sits_beside_the_main_one():
    assert browser_logfile(Path("logs/client_a_cl0.txt")) == Path("logs/client_a_cl0_cef.txt")


def test_only_console_lines_survive_the_filter():
    found = console_messages([CONSOLE_READY, CHROMIUM_NOISE, CONSOLE_CSP, CONSOLE_PARENTHESISED])

    assert len(found) == 3


def test_a_console_line_keeps_its_text_and_source_without_the_prefix():
    found = console_messages([CONSOLE_READY])

    assert found[0].startswith("INFO ")
    assert "clientData.setData is ready" in found[0]
    assert "reui://ui/assets/index-DHzRc2ja.js (10)" in found[0]
    # the [pid:tid:stamp:LEVEL:CONSOLE:line] prefix is gone
    assert "[29472" not in found[0]


def test_a_policy_refusal_is_kept():
    found = console_messages([CONSOLE_CSP])

    assert len(found) == 1
    assert "Content Security Policy" in found[0]


def test_the_parenthesised_console_form_is_recognised():
    found = console_messages([CONSOLE_PARENTHESISED])

    assert found == ['ERROR "boom", source: https://ugc-frame-x/index.html (42)']


def test_chromium_chatter_is_dropped():
    assert console_messages([CHROMIUM_NOISE]) == []
