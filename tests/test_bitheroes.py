"""Bit Heroes bot checks without a browser: python3 tests/test_bitheroes.py"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tab_autoclicker import bitheroes as bh, imaging  # noqa: E402

failed = 0


def check(ok: bool, what: str) -> None:
    global failed
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")


def page_with(*names: str) -> imaging.Image:
    """A grey page with the given reference images pasted at their places."""
    w, h = bh.PAGE_SIZE
    px = bytearray(b"\x40\x40\x48" * (w * h))
    for name in names:
        tpl, (x, y, tw, th) = bh.references()[name]
        for r in range(tpl.height):
            px[((y + r) * w + x) * 3:((y + r) * w + x + tpl.width) * 3] = tpl.rgb[r * tpl.width * 3:(r + 1) * tpl.width * 3]
    return imaging.Image(w, h, bytes(px))


def main() -> int:
    print("Recognising screens")
    for screen, refs in bh.SCREENS:
        got = bh.identify(page_with(*[r for r in refs if not r.startswith("!")]))
        check(got == screen, f"{screen} is recognised ({got})")
    check(bh.identify(page_with()) is None, "an empty page is not a known screen")
    check(bh.identify(page_with("rerun", "town_right")) is None,
          "Rerun and Town without the Cleared banner (sliding in): wait, don't press Town")

    print("Decisions")
    b = bh.Brain(bh.BotSettings(order=["pvp", "gauntlet"], enabled=["pvp", "gauntlet"], rest_min=10))
    steps = [("main", 1), ("pvp_window", 2), ("select_opponent", 3), ("team", 4), ("result_middle", 5)]
    got = [b.decide(s, t).point for s, t in steps]
    check(got == [bh.ICON["pvp"], (584, 287), (638, 214), (550, 487), (443, 498)] and b.runs["pvp"] == 1,
          f"plays PvP: icon, Play, Fight, Accept, Town ({got})")
    b.decide("result_middle", 6)
    check(b.runs["pvp"] == 1, "a result screen seen twice counts once")
    d = b.decide("not_enough_tickets", 7)
    check(d.point == bh.NO and b.mode == "gauntlet", f"out of tickets: No, then Gauntlet ({d.what})")
    d = b.decide("pvp_window", 8)
    check(d.point == (688, 120), "closes the PvP window afterwards")
    check(b.decide("main", 9).point == bh.ICON["gauntlet"], "opens Gauntlet from town")
    d = b.decide("not_enough_tokens", 10)
    check(b.mode is None and "resting" in d.what, f"all modes out: rests ({d.what})")
    check(b.decide("gauntlet_window", 11).point == (688, 123), "closes windows while resting")
    check(b.decide("main", 12).point is None, "waits in town while resting")
    check(b.decide("main", 12 + 10 * 60).point == bh.ICON["pvp"], "starts again with the first mode after the rest")

    b = bh.Brain(bh.BotSettings(order=["raid", "wb", "quest"], enabled=["raid", "wb", "quest"]))
    check(b.decide("raid_difficulty", 1).point == (628, 244), "raid: Heroic by default")
    t = bh.Brain(bh.BotSettings(order=["raid", "quest"], enabled=["raid", "quest"]))
    t.decide("team", 1)
    t.decide("not_enough_shards", 2)
    check(t.decide("team", 3).point == bh.TEAM_CLOSE and t.decide("raid_difficulty", 4).point == (748, 112),
          "out of shards from the team screen: closes the team and raid windows (no Accept loop)")
    check(t.decide("quest_map", 5).point == (430, 300) and t.decide("team", 7).point == (550, 487),
          "and accepts the next mode's team again")
    check(b.decide("cleared", 2).point == (365, 498) and b.runs["raid"] == 1, "raid cleared: Rerun")
    b.decide("not_enough_shards", 3)
    check(b.decide("cleared", 4).point == (524, 498), "cleared after running out: Town")
    check(b.decide("wb_lobby", 5).point == (405, 491), "World Boss lobby: Start")
    check(b.decide("wb_result", 6).point == (444, 498), "World Boss won: Regroup")
    check(b.decide("wb_defeat", 6.5).point == (365, 498), "World Boss lost: Regroup (on the left there)")
    b.decide("not_enough_xeals", 7)
    check(b.decide("wb_lobby", 8).point == (772, 82) and b.decide("quit_battle", 9).point == bh.YES,
          "leaves the World Boss lobby when it is out of xeals")
    check(b.decide("quest_map", 10).point == (430, 300) and b.decide("dungeon_window", 11).point == (510, 470),
          "Quest: dungeon, Enter")
    check(b.decide("disconnected", 12).point == (427, 377), "disconnected: Reconnect")
    check(b.decide("still_there", 12).point == (427, 377), "“Are you still there?”: Yes")
    check(b.decide(None, 13).point is None, "unknown screens (battles): waits")
    b.decide("victory_continue", 20)
    check(b.decide("victory_continue", 25).point is None and b.decide("victory_continue", 31).point == (500, 498),
          "mid-run victory: leaves it to autopilot, helps after 10 s")
    v = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_s=30))
    check(v.decide("main", 0).point == bh.ICON["invasion"] and v.decide("invasion_window", 1).point == (584, 292),
          "Invasion: icon, Play")
    v.decide("team", 2)
    v.decide("team_not_full", 3)
    check(v.decide("autopilot_on", 5).point is None and v.decide("autopilot_on", 20).point is None,
          "Invasion: lets autopilot fight at first")
    d = v.decide("autopilot_on", 36)
    check(d.key == bh.ESCAPE and d.point is None, f"after 30 s: presses Esc ({d.what})")
    check(v.decide("autopilot_on", 38).point is None and v.decide("autopilot_on", 38).key is None,
          "and waits for the quit popup")
    check(v.decide("quit_battle", 39).point == bh.YES and v.esc_works is True, "the popup came: Yes; Esc works")
    check(v.decide("result_middle", 40).point == (443, 498) and v.runs["invasion"] == 1, "and collects: Town")
    for s, t in (("main", 41), ("invasion_window", 42), ("team", 43)):
        v.decide(s, t)
    v.decide("autopilot_on", 44)
    check(v.decide("autopilot_on", 75).key == bh.ESCAPE, "the next Invasion: Esc again")
    check(v.decide("autopilot_on", 81).key == bh.ESCAPE and v.decide("autopilot_on", 87).point == bh.AUTO_BUTTON
          and v.esc_works is True, "Esc lost once: tries again, then autopilot off for this battle only")

    x = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_s=30))
    for s, t in (("main", 0), ("invasion_window", 1), ("team", 2)):
        x.decide(s, t)
    x.decide("autopilot_on", 5)
    check(x.decide("autopilot_on", 36).key == bh.ESCAPE and x.decide("autopilot_on", 41).key == bh.ESCAPE,
          "a game that ignores Esc: Esc twice")
    d = x.decide("autopilot_on", 46)
    check(d.point == bh.AUTO_BUTTON and x.esc_works is False, f"then autopilot off ({d.what})")
    check(x.decide("autopilot_off", 47).point == bh.BATTLE_X and x.decide("quit_battle", 48).point == bh.YES,
          "then leaves the battle with the X and confirms")
    check(x.esc_works is False, "the popup after the X does not count for Esc")
    x.decide("result_middle", 50)
    for s, t in (("main", 51), ("invasion_window", 52), ("team", 53)):
        x.decide(s, t)
    x.decide("autopilot_on", 54)
    check(x.decide("autopilot_on", 85).point == bh.AUTO_BUTTON, "later battles: autopilot off straight away")
    check(x.decide("autopilot_off", 100).point == bh.BATTLE_X, "and the X")
    x.decide("result_middle", 101)
    check(x.decide("autopilot_off", 150).point == bh.AUTO_BUTTON, "a later battle with autopilot off: switches it on")

    f = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_s=30, battle_speed=3))
    for s, t in (("main", 0), ("invasion_window", 1), ("team", 2)):
        f.decide(s, t)
    f.decide("autopilot_on", 5)
    check(f.decide("autopilot_on", 14).key is None and f.decide("autopilot_on", 15).key == bh.ESCAPE,
          "at 3× speed, 30 s of game time take 10 s")
    w = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_s=30))
    check(all(w.decide("autopilot_on", t).point is None for t in (0, 40, 400)),
          "a battle the bot did not start (e.g. a dungeon already running) is left alone")
    q = bh.Brain(bh.BotSettings(order=["quest"], enabled=["quest"]))
    q.decide("team", 1)
    check(all(q.decide("autopilot_on", t).point is None for t in (2, 100, 1000)), "Quest: never leaves early")
    check(q.decide("dungeon_autopilot_off", 1001).point == bh.AUTO_BUTTON,
          "a dungeon with autopilot off (after Invasion): switches it on")

    print("Dialogs")
    data = os.path.join(os.path.dirname(__file__), "data")
    shot = imaging.decode_png(open(os.path.join(data, "dialog.png"), "rb").read())
    check(bh.identify(shot) == "dialog", f"a boss's dialog in a dungeon is recognised ({bh.identify(shot)})")
    mine = imaging.decode_png(open(os.path.join(data, "dialog_player.png"), "rb").read())
    check(bh.identify(mine) == "dialog", f"your own character talking, picture on the right ({bh.identify(mine)})")
    after = imaging.decode_png(open(os.path.join(data, "battle_after_dialog.png"), "rb").read())
    check(bh.identify(after) != "dialog", f"the battle after it is not a dialog ({bh.identify(after)})")
    check(bh.Brain(bh.BotSettings()).decide("dialog", 1).point == bh.DIALOG_NEXT, "dialog: clicks the arrow")
    lobby_shot = imaging.decode_png(open(os.path.join(data, "wb_lobby.png"), "rb").read())
    check(bh.identify(lobby_shot) != "dialog", "the World Boss lobby is not a dialog")

    print("Choosing the raid, World Boss and zone")

    def load(name):
        return imaging.decode_png(open(os.path.join(data, name), "rb").read())
    check(bh.carousel(load("raid_1.png")) == (1, 17), f"raid window: raid 1 of 17 ({bh.carousel(load('raid_1.png'))})")
    check(bh.carousel(load("raid_2.png")) == (2, 17), f"after ▶: raid 2 of 17 ({bh.carousel(load('raid_2.png'))})")
    check(bh.carousel(load("wb_carousel.png")) == (9, 11), f"World Boss 9 of 11 ({bh.carousel(load('wb_carousel.png'))})")
    check(bh.carousel(lobby_shot) is None, "no dots in the lobby")
    for n, want in ((1, (1, False, True)), (12, (12, True, True)), (20, (20, True, False))):
        got = bh.zone(load(f"zone_{n:02d}.png"))
        check(got == want, f"zone map: zone {n}, arrows ◀ {want[1]} ▶ {want[2]} ({got})")
    check(bh.zone(lobby_shot)[0] is None, "no zone title in the lobby")
    r = bh.Brain(bh.BotSettings(order=["raid"], enabled=["raid"], raid_number=3))
    d = r.decide("raid_window", 1, dots=(1, 17))
    check(d.point == bh.CAROUSEL_NEXT and d.stays, f"raid 3 wanted, 1 shown: ▶ ({d.what})")
    check(r.decide("raid_window", 2, dots=(5, 17)).point == bh.CAROUSEL_PREV, "5 shown: ◀")
    check(r.decide("raid_window", 3, dots=(3, 17)).point == (575, 392), "3 shown: Summon")
    r9 = bh.Brain(bh.BotSettings(order=["raid"], enabled=["raid"], raid_number=20))
    d = r9.decide("raid_window", 1, dots=(4, 17))
    check(d.point is None and "only 17" in d.what and r9.decide("raid_window", 2, dots=(4, 17)).point == (575, 392),
          f"a raid that does not exist: says so once, then plays the one shown ({d.what})")
    r0 = bh.Brain(bh.BotSettings(order=["raid"], enabled=["raid"]))
    check(r0.decide("raid_window", 1, dots=(4, 17)).point == (575, 392), "raid 0 (default): the one shown")
    w = bh.Brain(bh.BotSettings(order=["wb"], enabled=["wb"], wb_boss=2))
    check(w.decide("wb_carousel", 1, dots=(9, 11)).point == bh.CAROUSEL_PREV
          and w.decide("wb_carousel", 2, dots=(2, 11)).point == (575, 393), "World Boss 2: ◀ until it shows, then Summon")
    z = bh.Brain(bh.BotSettings(order=["quest"], enabled=["quest"], quest_zone=12, quest_point=(500, 200)))
    check(z.decide("quest_map", 1, zone=(1, False, True)).point == bh.ZONE_NEXT, "zone 12 wanted, in zone 1: ▶")
    check(z.decide("quest_map", 2, zone=(20, True, False)).point == bh.ZONE_PREV, "in zone 20: ◀")
    check(z.decide("quest_map", 3, zone=(None, True, True)).point == bh.ZONE_PREV, "an unknown zone: ◀ toward the known ones")
    check(z.decide("quest_map", 4, zone=(12, True, True)).point == (500, 200), "in zone 12: the dungeon")
    z2 = bh.Brain(bh.BotSettings(order=["quest"], enabled=["quest"], quest_zone=18))
    d = z2.decide("quest_map", 1, zone=(15, True, False))
    check(d.point is None and "not open" in d.what and z2.decide("quest_map", 2, zone=(15, True, False)).point == (430, 300),
          f"a zone not open yet: says so once, then plays where it is ({d.what})")
    check(bh.Brain(bh.BotSettings(order=["raid", "quest"], enabled=["raid", "quest"], quest_zone=5))
          .decide("quest_map", 1, zone=(1, False, True)).point == (772, 75), "zone map while playing Raid: close it")
    s = bh.BotSettings.from_dict({"raid_number": 3, "wb_boss": 99, "quest_zone": 40})
    check((s.raid_number, s.wb_boss, s.quest_zone) == (3, 30, 20), "raid / boss / zone settings are kept in range")

    print("Gauntlet / Trials difficulty and Invasion wave")
    check(bh.read_numbers(load("gauntlet_window.png"), bh.FIELD_BOX) == (14578,), "reads the Gauntlet difficulty")
    check(bh.read_numbers(load("invasion_window.png"), bh.FIELD_BOX) == (58,), "reads the Invasion wave")
    check(bh.list_rows(load("gauntlet_ranges.png"))[1] == (12488, 13494), "reads a range row")
    check(bh.list_rows(load("gauntlet_values_dragged.png")) == [(13054,), (13044,), (13034,), (13024,), (13014,)],
          "reads a value list")
    check(bh.identify(load("wave_list.png")) == "pick_list" and bh.identify(load("gauntlet_ranges.png")) == "pick_list",
          "drop-down lists are recognised")
    check(bh.identify(load("gauntlet_window.png")) == "gauntlet_window"
          and bh.identify(load("invasion_window.png")) == "invasion_window", "the windows under them still are")
    check(bh.scroll_handle(load("gauntlet_values_dragged.png")) == (275, 25), "finds the scroll bar handle")

    def info(name):
        img = load(name)
        return bh.look_closer(img, bh.identify(img))
    g = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], gauntlet_difficulty=13470))
    d = g.decide("gauntlet_window", 1, **info("gauntlet_window.png"))
    check(d.point == bh.FIELD_OPEN, f"difficulty 14,578 set, 13,470 wanted: opens the list ({d.what})")
    d = g.decide("pick_list", 2, **info("gauntlet_ranges.png"))
    check(d.point == bh.list_row_center(1) and d.stays, f"opens the range 12,488 – 13,494 ({d.what})")
    d = g.decide("pick_list", 3, **info("gauntlet_values_top.png"))
    check(d.point == bh.list_row_center(2) and g.picking is None, f"13,494 – 13,454 shown: clicks 13,474 ({d.what})")
    check(g.decide("gauntlet_window", 4, field=13474).point == (584, 292), "then Play")
    g4 = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], gauntlet_difficulty=13020))
    g4.decide("gauntlet_window", 1, field=14578)
    g4.decide("pick_list", 2, **info("gauntlet_ranges.png"))
    d = g4.decide("pick_list", 3, **info("gauntlet_values_top.png"))
    check(d.point == bh.LIST_CLOSE and "too far down" in d.what,
          f"13,020 is not among the rows shown: closes the list ({d.what})")
    check(g4.decide("gauntlet_window", 4, field=14578).point == (584, 292), "and plays as set, without trying again")
    g6 = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], gauntlet_difficulty=5000))
    g6.decide("gauntlet_window", 1, field=14578)
    d = g6.decide("pick_list", 2, **info("gauntlet_ranges.png"))
    check(d.point == bh.LIST_CLOSE, f"a range below the ones shown: closes the list ({d.what})")
    g7 = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], gauntlet_difficulty=99999))
    g7.decide("gauntlet_window", 1, field=14578)
    check(g7.decide("pick_list", 2, **info("gauntlet_ranges.png")).point == bh.list_row_center(0)
          and g7.decide("pick_list", 3, **info("gauntlet_values_top.png")).point == bh.list_row_center(0),
          "above the hardest: the hardest range, its highest value")
    g5 = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], gauntlet_difficulty=13470))
    g5.decide("gauntlet_window", 1, field=14578)
    g5.decide("pick_list", 2, **info("gauntlet_ranges.png"))
    g5.decide("pick_list", 3, **info("gauntlet_values_top.png"))
    d = g5.decide("gauntlet_window", 4, field=13434)
    check(d.point is None and "13,434" in d.what and g5.decide("gauntlet_window", 5, field=13434).point == (584, 292),
          f"the game set another value than clicked: says so once, then plays ({d.what})")
    g2 = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], gauntlet_difficulty=14580))
    check(g2.decide("gauntlet_window", 1, field=14578).point == (584, 292), "within 10 of the wanted one: Play at once")
    t = bh.Brain(bh.BotSettings(order=["gauntlet"], enabled=["gauntlet"], trials_difficulty=500))
    check(t.decide("trials_window", 1, field=900).point == bh.FIELD_OPEN and t.decide("gauntlet_window", 2, field=900).point
          == (584, 292), "Trials has its own setting (Gauntlet left as it is)")
    v = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_wave=55))
    check(v.decide("invasion_window", 1, **info("invasion_window.png")).point == bh.FIELD_OPEN, "wave 58 set, 55 wanted: opens")
    d = v.decide("pick_list", 2, **info("wave_list.png"))
    check(d.point == bh.list_row_center(3), f"58 – 54 shown: clicks wave 55 ({d.what})")
    v3 = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_wave=30))
    v3.decide("invasion_window", 1, field=58)
    d = v3.decide("pick_list", 2, **info("wave_list.png"))
    check(d.point == bh.LIST_CLOSE, f"wave 30 is not among the rows shown: closes the list ({d.what})")
    v2 = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_wave=70))
    v2.decide("invasion_window", 1, field=58)
    d = v2.decide("pick_list", 2, **info("wave_list.png"))
    check(d.point == bh.list_row_center(0) and "highest" in d.what, f"wave 70 not open: takes the highest ({d.what})")
    check(v2.decide("invasion_window", 3, field=58).point == (584, 292), "and does not try again")
    x = bh.Brain(bh.BotSettings())
    check(x.decide("pick_list", 1, **info("wave_list.png")).point == bh.LIST_CLOSE, "a list the bot did not open: closed")

    print("World Boss team")
    lobby = imaging.decode_png(open(os.path.join(os.path.dirname(__file__), "data", "wb_lobby.png"), "rb").read())
    check(bh.identify(lobby) == "wb_lobby", "the lobby screenshot is recognised")
    check(bh.wb_team(lobby) == (1, 4), f"alone in a team of 4 ({bh.wb_team(lobby)})")
    joined = bytearray(lobby.rgb)
    for y in range(240, 266):           # someone takes row 2: its Invite button goes
        for xx in range(360, 482):
            joined[(y * lobby.width + xx) * 3:(y * lobby.width + xx) * 3 + 3] = b"\x28\x2f\x37"
    joined = imaging.Image(lobby.width, lobby.height, bytes(joined))
    check(bh.wb_team(joined) == (2, 4), f"a second player joins ({bh.wb_team(joined)})")
    check(bh.wb_team(page_with("wb_start", "wb_private")) == (0, 0), "no team rows: nothing counted")
    g = bh.Brain(bh.BotSettings(order=["wb"], enabled=["wb"], wb_players=2))
    d = g.decide("wb_lobby", 1, bh.wb_team(lobby))
    check(d.point is None and "1 of 2" in d.what, f"waits for 2 players ({d.what})")
    check(g.decide("wb_lobby", 2, bh.wb_team(joined)).point == (405, 491), "2 players: Start")
    g5 = bh.Brain(bh.BotSettings(order=["wb"], enabled=["wb"], wb_players=5))
    check(g5.decide("wb_lobby", 1, (4, 4)).point == (405, 491), "a team of 4 is full even if 5 were asked for")
    g1 = bh.Brain(bh.BotSettings(order=["wb"], enabled=["wb"]))
    check(g1.decide("wb_lobby", 1, (1, 4)).point == (405, 491), "1 player (the default): Start at once")
    s = bh.BotSettings.from_dict({"wb_players": 9, "battle_speed": 0})
    check((s.wb_players, s.battle_speed) == (5, 1) and bh.BotSettings.from_dict(s.to_dict()) == s,
          "settings are kept within range and saved")
    print("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
