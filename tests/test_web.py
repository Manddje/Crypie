from datetime import UTC, datetime

from app.strategy.describe import format_value
from app.strategy.model import DEFAULT_STRATEGY
from app.web import (
    ago,
    ago_html,
    amount,
    close_reason,
    condition_nl,
    dt,
    eur,
    indicator_nl,
    linkify,
    md,
    nl,
    num,
    order_reason,
    pct,
    pnl_class,
    price,
    price_eur,
    time_html,
    until,
    until_html,
)


def test_dutch_number_formats():
    assert eur(1234.5) == "€\u00a01.234,50"  # no-break space: the amount never wraps
    assert eur(-3.2) == "-€\u00a03,20"
    assert eur(None) == "–"
    assert pct(2.345) == "+2,3%"
    assert pct(-0.5) == "-0,5%"
    assert pct(67, 0, signed=False) == "67%"
    assert price(45000.123) == "45.000,12"
    assert price(0.5234) == "0,5234"
    assert amount(0.05) == "0,05"
    assert amount(1.0) == "1"
    assert amount(1234.5) == "1.234,5"


def test_dates_and_relative_time():
    moment = datetime(2026, 10, 31, 11, 5, tzinfo=UTC)
    assert dt(moment) == "31 okt 11:05 UTC"
    assert dt(moment, with_year=True) == "31 okt 2026 11:05 UTC"
    assert dt(None) == "–"
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert ago(now, now) == "zojuist"
    assert ago(moment, now) == "55 min geleden"
    assert ago(datetime(2026, 10, 31, 8, 0, tzinfo=UTC), now) == "4 uur geleden"
    assert ago(datetime(2026, 10, 30, 8, 0, tzinfo=UTC), now) == "gisteren"
    assert ago(None) == "nog niet"


def test_planned_moment_reads_nu_once_due():
    """The next cycle is planned: once due it reads 'nu', never 'zojuist' or 'geleden'."""
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert until(now, now) == "nu"
    assert until(datetime(2026, 10, 31, 11, 0, tzinfo=UTC), now) == "nu"
    assert until(datetime(2026, 10, 31, 12, 45, tzinfo=UTC), now) == "over 45 min"
    assert until(None) == "nog niet"
    html = str(until_html(now, now))
    assert "data-ahead" in html and ">nu</time>" in html


def test_labels_and_strategy_in_words():
    assert nl("paper") == "Oefenmodus"
    assert nl("buy") == "Kopen"
    assert nl("stop_loss") == "Stop-loss"
    assert nl("iets_onbekends") == "iets_onbekends"
    assert [indicator_nl(i) for i in DEFAULT_STRATEGY.indicators] == [
        "RSI 14",
        "EMA 200",
        "% onder hoogste koers van 168 candles",
    ]
    assert [condition_nl(c) for c in DEFAULT_STRATEGY.buy] == [
        "rsi14 lager dan 35",
        "rsi14 stijgt t.o.v. 1 candle eerder",
        "koers hoger dan ema200 (zacht filter)",
        "dd7d minstens 5 (zacht filter)",
    ]


# -- Foundation filters (UX audit) ---------------------------------------------------------------


def test_results_never_show_negative_zero_and_colour_follows_the_rounded_value():
    assert pct(-0.04) == "0,0%" and pct(0.04) == "0,0%"
    assert eur(-0.004) == "€ 0,00" and eur(2.5, signed=True) == "+€ 2,50"
    assert num(-0.001) == "0,00"
    assert pnl_class(-0.004) == "zero" and pnl_class(-0.006) == "neg" and pnl_class(3) == "pos"
    assert pnl_class(-0.04, 1) == "zero" and pnl_class(None) == ""
    assert eur(float("nan")) == "–" and pnl_class(float("inf")) == ""


def test_prices_keep_four_significant_digits():
    assert price(69498.123) == "69.498,12"
    assert price(5.1234) == "5,123"
    assert price(0.0000041495) in ("0,000004149", "0,000004150")
    assert price(0.00000394) == "0,000003940"
    assert price_eur(0.5) == "€ 0,5000"
    # The checklist ('… t.o.v. …') no longer turns cheap coins into 0,0000.
    assert format_value(0.0012) == "0,0012"
    assert format_value(0.00000394) == "0,00000394"
    assert format_value(0.5234) == "0,5234" and format_value(69498.1) == "69.498"
    assert format_value(41.12) == "41,1" and format_value(35, threshold=True) == "35"


def test_moments_are_utc_markup_that_the_browser_localises():
    moment = datetime(2026, 10, 31, 11, 5, tzinfo=UTC)
    html = str(time_html(moment))
    assert html == '<time datetime="2026-10-31T11:05:00Z" data-local="">31 okt 11:05 UTC</time>'
    assert 'data-local="year"' in time_html(moment, with_year=True)
    assert time_html(None) == "–" and hasattr(time_html(moment), "__html__")
    assert 'datetime="2026-10-31T11:05:00Z"' in time_html("2026-10-31T11:05:00+00:00")
    assert 'datetime="2026-10-31T11:05:00Z"' in time_html(int(moment.timestamp()))
    later = datetime(2026, 10, 31, 11, 10, tzinfo=UTC)
    rel = str(ago_html(moment, later))
    assert (
        rel.startswith('<time datetime="2026-10-31T11:05:00Z" data-ts') and "5 min geleden" in rel
    )
    assert ago(later, moment) == "over 5 min" and ago(None) == "nog niet"


def test_markdown_is_escaped_first_and_only_a_safe_subset_is_rendered():
    text = (
        "## Resultaat\nDe bot **verloor** 2% <script>alert(1)</script>\n\n"
        "### Wat ging goed\n- *rsi14* hield\n- stop_loss_pct bleef\n\n1. een\n2. twee\n\n"
        '[klik](javascript:alert(1)) ![x](https://evil.test/x.png) <img src=x onerror="alert(1)">'
    )
    html = str(md(text, heading=4))
    assert (
        html.startswith('<div class="md"><h4>Resultaat</h4>') and "<h5>Wat ging goed</h5>" in html
    )
    assert "<strong>verloor</strong>" in html and "<em>rsi14</em>" in html
    assert "stop_loss_pct" in html  # underscores inside words are not emphasis
    assert "<ul><li>" in html and "<ol><li>een</li><li>twee</li></ol>" in html
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "<a " not in html and "href" not in html and "<img" not in html
    assert "javascript:" not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html
    assert md("") == "" and md(None) == ""


def test_close_and_order_reasons_in_dutch():
    assert close_reason("manual") == "handmatig verkocht"
    assert close_reason("trailing_stop") == "trailing stop geraakt"
    assert nl("trailing_stop") == "Trailing stop" and nl("pending") == "Bezig"
    assert order_reason("claude: dip gekocht") == {
        "source": "claude",
        "source_nl": "Claude",
        "text": "dip gekocht",
    }
    assert order_reason("stop_loss")["text"] == "stop-loss geraakt"
    assert order_reason("iets anders")["source"] == ""


def test_linkify_escapes_and_only_links_http_addresses():
    html = str(
        linkify('Zie https://example.com/a?b=1&c=2. En <b>"x"</b> javascript:alert(1)\nregel')
    )
    assert (
        '<a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">'
        in html
    )
    assert "</a>. En &lt;b&gt;&quot;x&quot;&lt;/b&gt; javascript:alert(1)\nregel" in html
    quoted = str(linkify('"https://x.test/p"'))
    assert 'href="https://x.test/p"' in quoted and quoted.endswith(">https://x.test/p</a>&quot;")
    assert linkify(None) == "" and "<a" not in str(linkify("ftp://x.test"))
