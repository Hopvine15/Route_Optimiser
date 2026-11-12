"""Orienteering route search algorithm on OpenStreetMap walking networks."""

from dataclasses import dataclass
import logging
from typing import Sequence
import networkx as nx

from hexroute.graph import get_nearest_node, get_nearest_nodes_batch, get_node_coords, index_graph_cells
from hexroute.grid import cell_to_latlng, latlng_to_cell
from hexroute.targets import HexStatus, ScoredHex

logger = logging.getLogger("hexroute.optimise")

# Maximum number of target cells considered by the optimiser.
# Cells beyond this are pruned (lowest-scored first) to keep the
# O(T²) greedy-insertion loop tractable for large budgets.
MAX_TARGET_CELLS = 25


@dataclass
class RoutePlan:
    path_nodes: list[int]
    total_distance_m: float
    claimed_hexes: set[str]
    newly_claimed_hexes: set[str]
    existing_our_hexes: set[str]
    total_score: float
    waypoints: list[int]
    greedy_score: float
    is_loop: bool


class DistanceMatrix:
    """Lazily computes and caches shortest paths between key nodes."""

    def __init__(self, graph: nx.MultiDiGraph):
        self.graph = graph
        self._dist_cache: dict[tuple[int, int], float] = {}
        self._path_cache: dict[tuple[int, int], list[int]] = {}

    def precompute_distances(self, source_nodes: set[int]) -> None:
        """Pre-compute shortest-path distances *between* the given nodes only.

        Runs one Dijkstra per source node but stores results only for
        targets that are also in source_nodes, keeping the cache small
        (O(N²) entries instead of O(N × |V|)).
        """
        targets = frozenset(source_nodes)
        for src in source_nodes:
            try:
                lengths = nx.single_source_dijkstra_path_length(self.graph, src, weight="length")
                for tgt in targets:
                    if tgt in lengths:
                        self._dist_cache[(src, tgt)] = float(lengths[tgt])
            except Exception:
                pass

    def get_dist_and_path(self, u: int, v: int) -> tuple[float, list[int]]:
        if u == v:
            return 0.0, [u]

        pair = (u, v)
        if pair in self._dist_cache:
            dist = self._dist_cache[pair]
            if pair in self._path_cache:
                return dist, self._path_cache[pair]
            # Distance is known but path isn't — compute just the path
            try:
                path = nx.shortest_path(self.graph, source=u, target=v, weight="length")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                path = []
            self._path_cache[pair] = path
            return dist, path

        # Neither distance nor path is cached
        try:
            path = nx.shortest_path(self.graph, source=u, target=v, weight="length")
            dist = float(nx.shortest_path_length(self.graph, source=u, target=v, weight="length"))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            dist = float("inf")
            path = []

        self._dist_cache[pair] = dist
        self._path_cache[pair] = path
        return dist, path

    def get_distance(self, u: int, v: int) -> float:
        return self.get_dist_and_path(u, v)[0]

    def get_path(self, u: int, v: int) -> list[int]:
        return self.get_dist_and_path(u, v)[1]


def compute_tour_distance(waypoints: Sequence[int], matrix: DistanceMatrix, is_loop: bool) -> float:
    """Calculate the total shortest-path distance for a sequence of waypoints."""
    if len(waypoints) < 2:
        return 0.0

    total = 0.0
    for i in range(len(waypoints) - 1):
        d = matrix.get_distance(waypoints[i], waypoints[i + 1])
        if d == float("inf"):
            return float("inf")
        total += d

    if is_loop and waypoints[-1] != waypoints[0]:
        d = matrix.get_distance(waypoints[-1], waypoints[0])
        if d == float("inf"):
            return float("inf")
        total += d

    return total


def _insertion_cost(
    tour: list[int],
    pos: int,
    node: int,
    matrix: DistanceMatrix,
    is_loop: bool,
) -> float:
    """
    Compute the *marginal* distance change of inserting ``node`` at ``pos``
    in ``tour``, without recalculating the entire tour distance.

    Returns the delta (new total minus old total), or ``inf`` if any segment
    is unreachable.  This is O(1) with cached lookups instead of O(n).
    """
    n = len(tour)
    if n == 0:
        return 0.0
    if n == 1:
        # Only one node in tour — just measure to/from it
        d = matrix.get_distance(tour[0], node)
        if is_loop:
            d += matrix.get_distance(node, tour[0])
        return d

    if pos >= n:
        # Append at end
        d_new = matrix.get_distance(tour[n - 1], node)
        if is_loop:
            d_new += matrix.get_distance(node, tour[0])
            d_old = matrix.get_distance(tour[n - 1], tour[0])
        else:
            d_old = 0.0
        return d_new - d_old
    elif pos == 0:
        # Prepend at start (unusual but handle it)
        d_new = matrix.get_distance(node, tour[0])
        return d_new
    else:
        # Insert between tour[pos-1] and tour[pos]
        d_old = matrix.get_distance(tour[pos - 1], tour[pos])
        d_new1 = matrix.get_distance(tour[pos - 1], node)
        d_new2 = matrix.get_distance(node, tour[pos])
        delta = d_new1 + d_new2 - d_old
        return delta


def _tour_delta(
    tour: list[int],
    pos: int,
    node: int,
    matrix: DistanceMatrix,
    is_loop: bool,
) -> float:
    """Alias for _insertion_cost for readability."""
    return _insertion_cost(tour, pos, node, matrix, is_loop)


def two_opt_tour(waypoints: list[int], matrix: DistanceMatrix, is_loop: bool) -> list[int]:
    """Apply 2-opt local search to reduce tour distance without changing visited nodes.

    Uses incremental edge-swap delta instead of full tour recomputation:
    reversing segment [i, k] only changes two edges (the ones at the cut points),
    so we compare d(a,c)+d(b,d) vs d(a,b)+d(c,d) with O(1) cached lookups.
    """
    if len(waypoints) <= 3:
        return waypoints

    best_tour = list(waypoints)
    best_dist = compute_tour_distance(best_tour, matrix, is_loop)
    improved = True

    while improved:
        improved = False
        n = len(best_tour)
        end_idx = n if is_loop else n - 1
        for i in range(1, end_idx - 1):
            a = best_tour[i - 1]
            b = best_tour[i]
            for k in range(i + 1, end_idx):
                c = best_tour[k]
                d = best_tour[k + 1] if (k + 1) < n else best_tour[0]

                # Old edges: (a,b) and (c,d).  New edges after reversal: (a,c) and (b,d)
                d_old = matrix.get_distance(a, b) + matrix.get_distance(c, d)
                d_new = matrix.get_distance(a, c) + matrix.get_distance(b, d)

                if d_new < d_old - 0.5:
                    best_tour = best_tour[:i] + best_tour[i : k + 1][::-1] + best_tour[k + 1 :]
                    best_dist = best_dist - d_old + d_new
                    improved = True
                    break
            if improved:
                break

    return best_tour


def plan_optimal_route(
    graph: nx.MultiDiGraph,
    start_lat: float,
    start_lng: float,
    budget_km: float,
    scored_targets: dict[str, ScoredHex],
    is_loop: bool = True,
    res: int = 8,
) -> RoutePlan:
    """
    Plan a distance-budgeted route that claims the most valuable hexes.
    Picks through-street nodes within target hexagons to avoid cul-de-sac spurs,
    uses Greedy ratio insertion + 2-opt reordering + local search, and eliminates redundant spurs.
    """
    budget_m = budget_km * 1000.0
    start_node = get_nearest_node(graph, start_lat, start_lng)

    matrix = DistanceMatrix(graph)
    node_cells, edge_cells = index_graph_cells(graph, res=res)

    # Pre-build reverse index: cell -> [node_ids] for O(1) lookup
    # (avoids scanning all 300K+ graph nodes for each target cell)
    cell_to_nodes: dict[str, list[int]] = {}
    for node, cell in node_cells.items():
        cell_to_nodes.setdefault(cell, []).append(node)

    # 1. Map each target cell with positive score to high-quality candidate nodes
    # Prioritise nodes with degree >= 2 (through-streets and intersections) to prevent dead-end spurs
    cell_candidates: dict[str, list[int]] = {}

    # Pre-sort by score descending so we process highest-value cells first
    # and can bail early once we've found MAX_TARGET_CELLS reachable ones.
    ranked_targets = sorted(
        ((cell, target) for cell, target in scored_targets.items() if target.final_score > 0.0),
        key=lambda kv: kv[1].final_score,
        reverse=True,
    )

    # Single-source shortest-path lengths from start_node (one Dijkstra instead of N)
    start_dists_out: dict[int, float] | None = None
    start_dists_back: dict[int, float] | None = None

    # Batch-resolve nearest graph nodes for all target coords at once.
    # osmnx.nearest_nodes rebuilds a k-d tree on every call, so batching
    # turns N tree-builds into 1.  Limit to a reasonable multiple of
    # MAX_TARGET_CELLS since many will be filtered by reachability.
    _batch_limit = MAX_TARGET_CELLS * 3
    batch_targets = ranked_targets[:_batch_limit]
    batch_coords = [(t.center_lat, t.center_lng) for _, t in batch_targets]
    batch_nodes = get_nearest_nodes_batch(graph, batch_coords) if batch_coords else []

    for idx, (cell, target) in enumerate(ranked_targets):
        if len(cell_candidates) >= MAX_TARGET_CELLS:
            logger.info(
                "Capped target cells at %d (scored target: %.1f). Skipping remaining %d cells.",
                MAX_TARGET_CELLS,
                target.final_score,
                len(ranked_targets) - len(cell_candidates),
            )
            break

        c_node = batch_nodes[idx]

        # Quick reachability filter from start node (uses cached single-source Dijkstra)
        if start_dists_out is None:
            try:
                start_dists_out = dict(nx.single_source_dijkstra_path_length(graph, start_node, weight="length"))
            except Exception:
                start_dists_out = {}
        d_out = start_dists_out.get(c_node, float("inf"))
        if d_out == float("inf"):
            continue

        if is_loop:
            if start_dists_back is None:
                try:
                    start_dists_back = dict(nx.single_source_dijkstra_path_length(graph, start_node, weight="length"))
                except Exception:
                    start_dists_back = {}
            d_back = start_dists_back.get(c_node, float("inf"))
        else:
            d_back = 0.0
        if d_out + d_back > budget_m:
            continue

        # O(1) lookup via pre-built reverse index instead of scanning all nodes
        nodes_in_cell = cell_to_nodes.get(cell, [])
        if not nodes_in_cell:
            cell_candidates[cell] = [c_node]
            continue

        through_nodes = [n for n in nodes_in_cell if graph.degree(n) >= 2]
        candidates = [c_node]  # just use the nearest node — simplest, 1 candidate per cell
        # If a through-node is available, prefer it (avoids cul-de-sac dead ends)
        if through_nodes:
            candidates = [through_nodes[0]]

        cell_candidates[cell] = candidates

    # 2. Greedy construction with through-node insertion
    # Pre-compute pairwise shortest-path distances among all candidate nodes
    # so the greedy loop only does O(1) dict lookups.  Each unique source node
    # needs one Dijkstra run; results are stored only for candidate targets
    # to keep the cache small.
    all_candidate_nodes: set[int] = {start_node}
    for cands in cell_candidates.values():
        all_candidate_nodes.update(cands)
    logger.info(
        "Pre-computing distances from %d unique candidate nodes...",
        len(all_candidate_nodes),
    )
    matrix.precompute_distances(all_candidate_nodes)

    tour_nodes = [start_node]
    unvisited_cells = set(cell_candidates.keys())
    claimed_target_cells: set[str] = set()

    start_cell = node_cells.get(start_node)
    if start_cell and start_cell in unvisited_cells:
        unvisited_cells.remove(start_cell)
        claimed_target_cells.add(start_cell)

    while unvisited_cells:
        best_cell: str | None = None
        best_node: int | None = None
        best_pos = -1
        best_ratio = -1.0

        current_dist = compute_tour_distance(tour_nodes, matrix, is_loop)

        for cell in list(unvisited_cells):
            score = scored_targets[cell].final_score
            cands = cell_candidates[cell]

            for pos in range(1, len(tour_nodes) + 1):
                for node in cands:
                    # Incremental cost: O(1) cached lookups instead of
                    # recalculating the entire tour distance.
                    delta = _tour_delta(tour_nodes, pos, node, matrix, is_loop)
                    if delta == float("inf"):
                        continue
                    cand_dist = current_dist + delta

                    if cand_dist <= budget_m:
                        safe_delta = max(delta, 1.0)
                        ratio = score / safe_delta

                        if ratio > best_ratio:
                            best_ratio = ratio
                            best_cell = cell
                            best_node = node
                            best_pos = pos

        if best_cell is not None and best_node is not None:
            tour_nodes.insert(best_pos, best_node)
            unvisited_cells.remove(best_cell)
            claimed_target_cells.add(best_cell)
        else:
            break

    greedy_score = sum(scored_targets[c].final_score for c in claimed_target_cells)

    # 3. 2-opt reordering
    tour_nodes = two_opt_tour(tour_nodes, matrix, is_loop)

    # 4. Secondary insertion search with remaining budget
    for cell in list(unvisited_cells):
        score = scored_targets[cell].final_score
        cands = cell_candidates[cell]
        best_pos = -1
        best_node = -1
        best_delta = float("inf")

        current_dist = compute_tour_distance(tour_nodes, matrix, is_loop)

        for pos in range(1, len(tour_nodes) + 1):
            for node in cands:
                delta = _tour_delta(tour_nodes, pos, node, matrix, is_loop)
                if delta == float("inf"):
                    continue
                cand_dist = current_dist + delta

                if cand_dist <= budget_m and delta < best_delta:
                    best_delta = delta
                    best_pos = pos
                    best_node = node

        if best_pos != -1 and best_node != -1:
            tour_nodes.insert(best_pos, best_node)
            unvisited_cells.remove(cell)
            claimed_target_cells.add(cell)

    # Run 2-opt once after all secondary insertions (not per-cell)
    tour_nodes = two_opt_tour(tour_nodes, matrix, is_loop)

    # 5. Construct complete path on graph
    full_path: list[int] = []
    waypoint_pairs = list(zip(tour_nodes, tour_nodes[1:]))
    if is_loop and len(tour_nodes) > 1 and tour_nodes[-1] != tour_nodes[0]:
        waypoint_pairs.append((tour_nodes[-1], tour_nodes[0]))

    for u, v in waypoint_pairs:
        segment = matrix.get_path(u, v)
        if not full_path:
            full_path.extend(segment)
        else:
            full_path.extend(segment[1:])

    if not full_path:
        full_path = [start_node]

    # 6. Extract all cells crossed by the complete path
    all_crossed_cells: set[str] = set()
    for i in range(len(full_path)):
        node = full_path[i]
        if node in node_cells:
            all_crossed_cells.add(node_cells[node])

        if i < len(full_path) - 1:
            next_node = full_path[i + 1]
            edge_data = graph.get_edge_data(node, next_node)
            if edge_data:
                for k in edge_data.keys():
                    key_tuple = (node, next_node, k)
                    if key_tuple in edge_cells:
                        all_crossed_cells.update(edge_cells[key_tuple])

    total_dist = compute_tour_distance(tour_nodes, matrix, is_loop)
    if total_dist == float("inf"):
        total_dist = 0.0

    # Categorise newly claimed vs existing team territory
    newly_claimed: set[str] = set()
    existing_ours: set[str] = set()

    for c in all_crossed_cells:
        t = scored_targets.get(c)
        if t and t.status == HexStatus.OURS:
            existing_ours.add(c)
        elif t and t.final_score > 0.0:
            newly_claimed.add(c)
        else:
            existing_ours.add(c)

    total_score = sum(scored_targets[c].final_score for c in newly_claimed if c in scored_targets)

    return RoutePlan(
        path_nodes=full_path,
        total_distance_m=total_dist,
        claimed_hexes=all_crossed_cells,
        newly_claimed_hexes=newly_claimed,
        existing_our_hexes=existing_ours,
        total_score=total_score,
        waypoints=tour_nodes,
        greedy_score=greedy_score,
        is_loop=is_loop,
    )
