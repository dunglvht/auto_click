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
    check(v.decide("autopilot_on", 36).point == bh.AUTO_BUTTON, "after 30 s: switches autopilot off")
    check(v.decide("autopilot_off", 37).point == bh.BATTLE_X and v.decide("quit_battle", 38).point == bh.YES,
          "then leaves the battle and confirms")
    check(v.decide("result_middle", 40).point == (443, 498) and v.runs["invasion"] == 1, "and collects: Town")
    check(v.decide("autopilot_off", 50).point == bh.AUTO_BUTTON, "a later battle with autopilot off: switches it on")
    w = bh.Brain(bh.BotSettings(order=["invasion"], enabled=["invasion"], invasion_s=30))
    check(all(w.decide("autopilot_on", t).point is None for t in (0, 40, 400)),
          "a battle the bot did not start (e.g. a dungeon already running) is left alone")
    q = bh.Brain(bh.BotSettings(order=["quest"], enabled=["quest"]))
    q.decide("team", 1)
    check(all(q.decide("autopilot_on", t).point is None for t in (2, 100, 1000)), "Quest: never leaves early")
    check(q.decide("dungeon_autopilot_off", 1001).point == bh.AUTO_BUTTON,
          "a dungeon with autopilot off (after Invasion): switches it on")
    print("ALL CHECKS PASSED" if not failed else f"{failed} CHECK(S) FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
