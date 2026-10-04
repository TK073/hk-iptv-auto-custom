"""离线回归测试：不打网络，只锁住已实测确认过的判定行为。

这些用例来自 2026-10-04 的实测结论，改动 main.py 时如果打破任何一条，
说明召回率或判活严格度回退了，应当先确认再改测试。
"""

import main


# ---------- 频道名归一与别名匹配（P0-1） ----------

def test_simplified_names_match_traditional_canonical():
    # main.py 里曾经只有繁體「無線/無綫」，简体写法全部漏配
    assert main.match_standard_channel_name("无线新闻台") == "無綫新聞台"
    assert main.match_standard_channel_name("TVB无线新闻") == "無綫新聞台"
    assert main.match_standard_channel_name("无线财经体育资讯台") == "無綫財經體育資訊台"


def test_longest_alias_wins_so_viutvsix_is_not_absorbed():
    # 'viutv' 是 ViuTV 的别名，短名抢先匹配会把 ViuTVsix 的条目吞进 ViuTV
    assert main.match_standard_channel_name("ViuTVsix") == "ViuTVsix"
    assert main.match_standard_channel_name("viutv 6") == "ViuTVsix"
    assert main.match_standard_channel_name("ViuTV") == "ViuTV"


def test_tai_variant_and_punctuation_are_folded():
    assert main.match_standard_channel_name("無綫新聞臺") == "無綫新聞台"
    assert main.match_standard_channel_name("港台电视31") == "港台電視31"
    assert main.match_standard_channel_name("翡翠台 (1080P)") == "翡翠台"
    assert main.match_standard_channel_name("HOY资讯台") == "HOY 資訊台"


def test_non_hk_names_are_not_matched():
    for name in ["广东新闻", "咪咕直播 「IPV4」", "凤凰资讯", "UNKNOWN", ""]:
        assert main.match_standard_channel_name(name) == ""


# ---------- 黑名单先于别名（用户明确「不收」的那些台） ----------

def _parse_lines(monkeypatch, lines):
    monkeypatch.setattr(main, "PLAYLIST_STATS", {})
    monkeypatch.setattr(main, "FETCH_STATS", {})
    monkeypatch.setattr(main, "fetch_raw_content", lambda url, timeout=12: "\n".join(lines))
    return main.parse_single_playlist("http://fake.test/list.m3u")


def test_blacklisted_channels_are_rejected(monkeypatch):
    lines = ["#EXTM3U"]
    for name in ["凤凰香港台", "TVB星河", "耀才财经", "浙江新闻", "福建新闻"]:
        lines += [f"#EXTINF:-1,{name}", f"http://fake.test/{abs(hash(name))}.m3u8"]
    got = _parse_lines(monkeypatch, lines)
    assert got == []


def test_blacklist_beats_alias_even_when_alias_would_hit(monkeypatch):
    # 「凤凰资讯台」不在别名表里；这条确认黑名单不会因为归一化而被绕过
    lines = ["#EXTM3U", "#EXTINF:-1,鳳凰資訊台", "http://fake.test/a.m3u8"]
    assert _parse_lines(monkeypatch, lines) == []


def test_parse_records_per_source_stats(monkeypatch):
    lines = ["#EXTM3U",
             "#EXTINF:-1,无线新闻台", "http://fake.test/a.m3u8",
             "#EXTINF:-1,广东新闻", "http://fake.test/b.m3u8"]
    got = _parse_lines(monkeypatch, lines)
    stats = main.PLAYLIST_STATS["http://fake.test/list.m3u"]
    assert len(got) == 1 and got[0]["name"] == "無綫新聞台"
    assert stats == {"entries": 2, "hits": 1, "v6": False}


# ---------- 同流去重键（P1） ----------

def test_dedupe_key_ignores_scheme_and_extension():
    a = main.stream_dedupe_key("https://rthktv32-live.akamaized.net/hls/live/2/RTHKTV32/master.m3u8")
    b = main.stream_dedupe_key("http://rthktv32-live.akamaized.net/hls/live/2/RTHKTV32/master.m3u8/")
    assert a == b
    c = main.stream_dedupe_key("http://61.10.2.141/live_freedirect/freehd209_h.live/playlist.m3u")
    d = main.stream_dedupe_key("http://61.10.2.141/live_freedirect/freehd209_h.live/playlist.m3u8")
    assert c == d


def test_dedupe_key_keeps_different_streams_apart():
    assert (main.stream_dedupe_key("http://php.jdshipin.com/TVOD/iptv.php?id=viutv")
            != main.stream_dedupe_key("http://php.jdshipin.com/TVOD/iptv.php?id=viutv2"))


# ---------- HTML 落地页识别（第 2 项） ----------

def test_looks_like_html_page():
    assert main.looks_like_html_page("<!doctype html>\n<html><body>x</body></html>") is True
    assert main.looks_like_html_page("<HTML><HEAD>x") is True
    assert main.looks_like_html_page("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1") is False
    # 反代会把合法清单标成 text/html，所以判定只看 body，且正文含 #EXTM3U 一律放行
    assert main.looks_like_html_page("<html><body>#EXTM3U ok</body></html>") is False
    assert main.looks_like_html_page("") is False
    assert main.looks_like_html_page("G@...binary ts payload...") is False


def test_blocked_url_tokens_never_hit_network(monkeypatch):
    calls = []
    monkeypatch.setattr(main.requests, "get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("不该发请求")))
    blocked = "https://rainews1-live.akamaized.net/hls/live/598326/rainews1/rainews1/playlist.m3u8"
    assert main.test_stream_speed(blocked)[0] is False
    assert calls == []


# ---------- 判活三道门（第 3 项的延迟上限 + ffprobe） ----------

def test_evaluate_stream_gate_order(monkeypatch):
    monkeypatch.setattr(main, "FFPROBE_BIN", "/usr/bin/ffprobe")
    monkeypatch.setattr(main, "REQUIRE_VIDEO_TRACK", True)

    monkeypatch.setattr(main, "test_stream_speed", lambda url, timeout=5: (False, 0, float("inf")))
    ok, _, _, kind, _ = main.evaluate_stream("http://x/y.m3u8")
    assert not ok and kind == "speed"

    monkeypatch.setattr(main, "test_stream_speed", lambda url, timeout=5: (True, 5.0, 5001.0))
    ok, _, _, kind, reason = main.evaluate_stream("http://x/y.m3u8")
    assert not ok and kind == "delay" and "5000" in reason

    monkeypatch.setattr(main, "test_stream_speed", lambda url, timeout=5: (True, 5.0, 4999.0))
    monkeypatch.setattr(main, "probe_video_track", lambda url, timeout=12: (False, "無視頻軌(audio)"))
    ok, _, _, kind, _ = main.evaluate_stream("http://x/y.m3u8")
    assert not ok and kind == "decode"

    monkeypatch.setattr(main, "probe_video_track", lambda url, timeout=12: (True, "h264 1920x1080 [hls]"))
    assert main.evaluate_stream("http://x/y.m3u8")[0] is True


def test_evaluate_stream_without_ffprobe_downgrades(monkeypatch):
    monkeypatch.setattr(main, "FFPROBE_BIN", None)
    monkeypatch.setattr(main, "test_stream_speed", lambda url, timeout=5: (True, 2.0, 300.0))
    monkeypatch.setattr(main, "probe_video_track",
                        lambda url, timeout=12: (_ for _ in ()).throw(AssertionError("无 ffprobe 时不该调用")))
    assert main.evaluate_stream("http://x/y.m3u8")[0] is True


# ---------- 输出格式（时间戳 / UA 注入 / logo 编码 / 官方源） ----------

def test_official_source_retries_within_round(monkeypatch, tmp_path, capsys):
    """官方源同轮重试：前两次抽样失败不该让保底台消失（跨轮记忆是被否决的，这里只是同轮内多次抽样）"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "FFPROBE_BIN", None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    target = main.OFFICIAL_CHANNELS[0]["url"]
    calls = {}

    def fake_evaluate(url):
        calls[url] = calls.get(url, 0) + 1
        if url == target and calls[url] < main.OFFICIAL_ATTEMPTS:
            return False, 0, float("inf"), "speed", "測速未通過"
        return True, 2.0, 300.0, "", ""

    monkeypatch.setattr(main, "evaluate_stream", fake_evaluate)
    main.generate_m3u([])

    out = (tmp_path / "hk_live.m3u").read_text(encoding="utf-8")
    assert target in out
    assert calls[target] == main.OFFICIAL_ATTEMPTS          # 失败两次后第三次才收录
    assert f"（第 {main.OFFICIAL_ATTEMPTS} 次通过）" in capsys.readouterr().out


def test_official_source_dropped_after_all_attempts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "FFPROBE_BIN", None)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "evaluate_stream",
                        lambda url: (False, 0, float("inf"), "speed", "測速未通過"))
    main.generate_m3u([])
    out = (tmp_path / "hk_live.m3u").read_text(encoding="utf-8")
    for off in main.OFFICIAL_CHANNELS:
        assert off["url"] not in out                        # 全灭就剔除，不写死链


def test_output_format(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "FFPROBE_BIN", None)
    monkeypatch.setattr(main, "evaluate_stream", lambda url: (True, 3.0, 200.0, "", ""))
    cands = [{"name": "翡翠台", "raw_name": "TVB 翡翠台", "url": "http://a.test/1.m3u8", "v6": False},
             {"name": "HOY 資訊台", "raw_name": "HOY 資訊台", "url": "http://b.test/2.m3u8", "v6": True},
             {"name": "翡翠台", "raw_name": "翡翠台", "url": "http://a.test/1.m3u8", "v6": False}]
    main.generate_m3u(cands)

    lines = (tmp_path / "hk_live.m3u").read_text(encoding="utf-8").splitlines()
    extinf = [l for l in lines if l.startswith("#EXTINF")]
    urls = [l for l in lines if l.startswith("http")]
    vlcopt = [l for l in lines if l.startswith("#EXTVLCOPT:http-user-agent=")]

    assert any(l.startswith("# Updated: ") and l.endswith("HKT") for l in lines)
    assert len(extinf) == len(urls) == len(vlcopt)      # 每条源都带 UA 行
    assert not any(any(ord(c) > 0x127 for c in l.split('tvg-logo="')[1].split('"')[0]) for l in extinf)
    assert 'group-title="Hong Kong [IPv6]"' in "".join(extinf)
    assert urls.count("http://a.test/1.m3u8") == 1      # 同流重复被去掉
    assert any(main.OFFICIAL_CHANNELS[0]["url"] in u for u in urls)   # 官方源在表内
