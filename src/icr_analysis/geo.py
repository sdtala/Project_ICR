"""Exact nearest-neighbour searches on a spherical Earth."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
import math

import numpy as np

EARTH_RADIUS_KM = 6371.0088


def latlon_to_unit(latitude: np.ndarray, longitude: np.ndarray) -> np.ndarray:
    """Convert WGS84 latitude/longitude degrees to unit-sphere Cartesian vectors."""
    lat = np.radians(np.asarray(latitude, dtype=float))
    lon = np.radians(np.asarray(longitude, dtype=float))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def chord_squared_to_km(distance_squared: float) -> float:
    chord = min(2.0, math.sqrt(max(0.0, distance_squared)))
    return 2.0 * EARTH_RADIUS_KM * math.asin(chord / 2.0)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance using the numerically stable haversine formula."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lon / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(slots=True)
class _Node:
    point_index: int
    axis: int
    left: int = -1
    right: int = -1


class SphericalIndex:
    """A deterministic exact k-d tree over unit-sphere Cartesian coordinates."""

    def __init__(self, latitude: np.ndarray, longitude: np.ndarray) -> None:
        self.points = latlon_to_unit(latitude, longitude)
        if len(self.points) == 0:
            raise ValueError("Cannot build a spatial index without points")
        self.nodes: list[_Node] = []
        indices = np.arange(len(self.points), dtype=int)
        self.root = self._build(indices, depth=0)

    def _build(self, indices: np.ndarray, depth: int) -> int:
        if len(indices) == 0:
            return -1
        axis = depth % 3
        middle = len(indices) // 2
        order = np.argpartition(self.points[indices, axis], middle)
        arranged = indices[order]
        node_position = len(self.nodes)
        self.nodes.append(_Node(point_index=int(arranged[middle]), axis=axis))
        left = self._build(arranged[:middle], depth + 1)
        right = self._build(arranged[middle + 1 :], depth + 1)
        self.nodes[node_position].left = left
        self.nodes[node_position].right = right
        return node_position

    def query(self, latitude: float, longitude: float, k: int = 1, exclude_index: int | None = None) -> list[tuple[int, float]]:
        if k <= 0:
            return []
        target = latlon_to_unit(np.array([latitude]), np.array([longitude]))[0]
        heap: list[tuple[float, int]] = []

        def visit(node_position: int) -> None:
            if node_position < 0:
                return
            node = self.nodes[node_position]
            point = self.points[node.point_index]
            delta = target - point
            distance_squared = float(np.dot(delta, delta))
            if node.point_index != exclude_index:
                entry = (-distance_squared, -node.point_index)
                if len(heap) < k:
                    heapq.heappush(heap, entry)
                elif entry > heap[0]:
                    heapq.heapreplace(heap, entry)

            axis_delta = float(target[node.axis] - point[node.axis])
            near = node.left if axis_delta <= 0 else node.right
            far = node.right if axis_delta <= 0 else node.left
            visit(near)
            worst_squared = -heap[0][0] if len(heap) == k else math.inf
            if axis_delta * axis_delta <= worst_squared:
                visit(far)

        visit(self.root)
        results = [(-item[1], chord_squared_to_km(-item[0])) for item in heap]
        return sorted(results, key=lambda item: (item[1], item[0]))

    def query_many(self, latitude: np.ndarray, longitude: np.ndarray, k: int = 1) -> list[list[tuple[int, float]]]:
        return [self.query(float(lat), float(lon), k=k) for lat, lon in zip(latitude, longitude, strict=True)]

