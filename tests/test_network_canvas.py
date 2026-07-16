"""Geometry regression tests for the size-aware hub/peer topology layout.

`network_canvas.py`'s box positions used to be fixed constants; they're now
derived from `available_width`/`available_height` (see `_agent_columns`,
`_hub_vertical_spacing`, `_compute_hub_layout`, `_compute_peer_layout`).
These tests check the alignment invariants that motivated the rewrite --
every junction/drop/arrowhead column must land exactly on its agent box's
true visual center (border columns included), agents must be evenly
spaced, and the cluster must be centered under the hub -- across a few
agent counts and panel sizes, not just eyeballing one rendered diagram.
"""

import unittest

from uagents_trace.network_canvas import (
    build_hub_topology,
    build_peer_topology,
    _compute_hub_layout,
    _compute_peer_layout,
)


def _hub_legs(n: int) -> tuple[list[dict], list[str]]:
    legs = [{"subagent": f"sub{i}", "state": "completed" if i % 2 == 0 else "pending"} for i in range(1, n + 1)]
    names = [f"SubAgent{i}" for i in range(1, n + 1)]
    return legs, names


class HubTopologyAlignmentTests(unittest.TestCase):
    """One test per agent count exercises the same invariants twice: once
    with no panel size (the pre-size-aware fallback) and once with a large
    panel (exercising the actual size-aware growth path).
    """

    def _assert_aligned(self, n: int, available_width=None, available_height=None):
        legs, names = _hub_legs(n)
        layout = _compute_hub_layout(legs, "Orchestrator", names, available_width, available_height)
        diagram = build_hub_topology(
            legs, "Orchestrator", names, available_width=available_width, available_height=available_height
        )
        lines = diagram.plain.split("\n")
        arrow_row = layout.agent_row - 1

        for i, cx in enumerate(layout.agent_centers):
            with self.subTest(agent=i, size=(available_width, available_height)):
                # Bus junction, drop line, and arrowhead all land on the
                # box's true visual center (border columns included) --
                # not one column off, which was the original bug.
                self.assertEqual(lines[layout.bus_row][cx], "┬")
                self.assertEqual(lines[arrow_row][cx], "▼")
                x0, y0, x1, _ = layout.agent_boxes[i]
                box_true_center = x0 + (x1 - x0) // 2
                self.assertEqual(box_true_center, cx)
                self.assertEqual(lines[y0][x0], "┌")
                self.assertEqual(lines[y0][x1 - 1], "┐")

        # Evenly spaced: every agent's column is the same distance from
        # the next -- exact equality, not just "roughly even".
        if len(layout.agent_centers) >= 2:
            deltas = {b - a for a, b in zip(layout.agent_centers, layout.agent_centers[1:])}
            self.assertEqual(len(deltas), 1, f"uneven spacing: {layout.agent_centers}")

        # Cluster horizontally centered under the hub (within 1 column --
        # unavoidable integer-rounding slack between two independently
        # centered spans of possibly-different width/parity).
        cluster_mid = (min(layout.agent_centers) + max(layout.agent_centers)) / 2
        self.assertLessEqual(abs(cluster_mid - layout.hub_cx), 1)

        # The bus line's endpoints sit directly above the outermost
        # agents' drop columns, not extending past or falling short of them.
        bus_span = [j for j, ch in enumerate(lines[layout.bus_row]) if ch in ("┬", "─")]
        self.assertEqual(min(bus_span), min(layout.agent_centers))
        self.assertEqual(max(bus_span), max(layout.agent_centers))

    def test_two_subagents_default_size(self):
        self._assert_aligned(2)

    def test_three_subagents_default_size(self):
        self._assert_aligned(3)

    def test_five_subagents_default_size(self):
        self._assert_aligned(5)

    def test_two_subagents_wide_panel(self):
        self._assert_aligned(2, available_width=120, available_height=30)

    def test_three_subagents_wide_panel(self):
        self._assert_aligned(3, available_width=120, available_height=30)

    def test_five_subagents_wide_panel(self):
        self._assert_aligned(5, available_width=160, available_height=30)

    def test_single_subagent(self):
        # len(agent_centers) == 1 takes a separate branch in
        # _draw_hub_arrows: a single sub-agent centers directly under the
        # hub, so it's a straight vertical line with no bus/junction row
        # at all (nothing to branch to) -- doesn't fit the general
        # n-agent invariants in _assert_aligned, so it's checked directly.
        legs, names = _hub_legs(1)
        layout = _compute_hub_layout(legs, "Orchestrator", names)
        diagram = build_hub_topology(legs, "Orchestrator", names)
        lines = diagram.plain.split("\n")

        cx = layout.agent_centers[0]
        self.assertEqual(cx, layout.hub_cx, "a single sub-agent should center directly under the hub")
        x0, y0, x1, _ = layout.agent_boxes[0]
        box_true_center = x0 + (x1 - x0) // 2
        self.assertEqual(box_true_center, cx)

        arrow_row = layout.agent_row - 1
        self.assertEqual(lines[arrow_row][cx], "▼")
        self.assertEqual(lines[y0][x0], "┌")
        self.assertEqual(lines[y0][x1 - 1], "┐")

    def test_wide_panel_diagram_is_visibly_larger_than_default(self):
        # The whole point of the rewrite: given real panel space, the
        # diagram actually grows (wider gaps, taller connectors) instead
        # of sitting at a small fixed size inside a big empty panel.
        legs, names = _hub_legs(3)
        default_layout = _compute_hub_layout(legs, "Orchestrator", names)
        wide_layout = _compute_hub_layout(legs, "Orchestrator", names, available_width=120, available_height=30)
        self.assertGreater(wide_layout.total_w, default_layout.total_w)
        self.assertGreater(wide_layout.total_h, default_layout.total_h)

    def test_narrow_panel_does_not_shrink_below_default(self):
        # A panel smaller than the natural minimum must fall back to the
        # same floor as omitting available_width/height entirely -- never
        # collapse boxes/gaps below the pre-size-aware look.
        legs, names = _hub_legs(5)
        default_layout = _compute_hub_layout(legs, "Orchestrator", names)
        narrow_layout = _compute_hub_layout(legs, "Orchestrator", names, available_width=10, available_height=5)
        self.assertEqual(narrow_layout.total_w, default_layout.total_w)
        self.assertEqual(narrow_layout.total_h, default_layout.total_h)

    def test_hit_regions_match_rendered_topology(self):
        # build_hub_hit_regions is a separate call from build_hub_topology
        # in live.py -- both must be given the same available_width/height
        # or clicks will target the wrong box. Confirmed here by checking
        # the hit region's own center matches the layout's agent_centers
        # (both derived from the same _compute_hub_layout call).
        from uagents_trace.network_canvas import build_hub_hit_regions

        legs, names = _hub_legs(4)
        regions = build_hub_hit_regions(legs, "Orchestrator", names, 130, 28)
        layout = _compute_hub_layout(legs, "Orchestrator", names, 130, 28)
        for (x0, _, x1, _), cx in zip(regions, layout.agent_centers):
            self.assertTrue(x0 <= cx < x1)


class PeerTopologyAlignmentTests(unittest.TestCase):
    def _assert_aligned(self, available_width=None, available_height=None):
        layout = _compute_peer_layout("Alice", "Bob", available_width, available_height)
        diagram = build_peer_topology(
            "Alice", "Bob", state="completed", available_width=available_width, available_height=available_height
        )
        lines = diagram.plain.split("\n")

        self.assertEqual(lines[layout.arrow_row][layout.right_cx], "▶")

        lx0, ly0, lx1, _ = layout.left_box
        rx0, ry0, rx1, _ = layout.right_box
        self.assertEqual(lx0 + (lx1 - lx0) // 2, layout.left_cx)
        self.assertEqual(rx0 + (rx1 - rx0) // 2, layout.right_cx)
        self.assertEqual(lines[ly0][lx0], "┌")
        self.assertEqual(lines[ry0][rx1 - 1], "┐")

    def test_default_size(self):
        self._assert_aligned()

    def test_wide_panel(self):
        self._assert_aligned(available_width=120, available_height=30)

    def test_wide_panel_is_visibly_wider_than_default(self):
        default_layout = _compute_peer_layout("Alice", "Bob")
        wide_layout = _compute_peer_layout("Alice", "Bob", available_width=120, available_height=30)
        self.assertGreater(wide_layout.total_w, default_layout.total_w)

    def test_narrow_panel_does_not_shrink_below_default(self):
        default_layout = _compute_peer_layout("Alice", "Bob")
        narrow_layout = _compute_peer_layout("Alice", "Bob", available_width=10, available_height=5)
        self.assertEqual(narrow_layout.total_w, default_layout.total_w)
        self.assertEqual(narrow_layout.total_h, default_layout.total_h)


if __name__ == "__main__":
    unittest.main()
