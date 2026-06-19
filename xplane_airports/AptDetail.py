"""
Tools for reading, inspecting, and manipulating X-Plane’s airport (apt.dat) files.
"""
import itertools
from contextlib import suppress
from dataclasses import dataclass, field
from operator import attrgetter
from os import PathLike
import re
from enum import IntEnum, Enum
from pathlib import Path
from typing import Callable, Collection, Dict, Iterable, List, Optional, Union, FrozenSet
from xplane_airports._cached_prop import apt_cached_property
from xplane_airports.AptDat import IcaoWidth, RowCode, AptDatLine, Airport

# ##################################
#
# Utility Classes
#
class Accessories:
    """Find all lines with line code in accessories related to a close, previous line with code main.

    Example:
    ...
    1202 1007 609 twoway taxiway_E B6
    1202 1022 1023 twoway taxiway_F B 9
    1204 departure 07L,25R
    1204 arrival 07L,25R
    1204 ils 07L,25R
    1202 627 1022 twoway taxiway_F B 9
    1204 departure 07L,25R
    1204 arrival 07L,25R
    1204 ils 07L,25R
    1202 610 847 twoway taxiway_E E5
    ...
    will build with main = 1202 (edges) and accessories = [ 1204 (active edges) ]:

    1007  609: nothing

    1022 1023:
        1204 departure 07L,25R  # lines that relate to above 1202 1022 1023 twoway taxiway_F B 9
        1204 arrival 07L,25R
        1204 ils 07L,25R

     627 1022:
        1204 departure 07L,25R  # lines that relate to above 1202 627 1022 twoway taxiway_F B 9
        1204 arrival 07L,25R
        1204 ils 07L,25R

     610  847: Nothing
    """

    @staticmethod
    def from_tokenized_lines(tokenized_lines: List[List[Union[RowCode, str]]], main: RowCode, accessories: List[RowCode]) -> Dict[List[Union[RowCode, str]], List[List[Union[RowCode, str]]]]:
        lines_with_accessories = {}
        main_line = None
        i = 0
        while i < len(tokenized_lines):
            tokens = tokenized_lines[i]
            if tokens[0] != main:
                i += 1
                continue
            main_line = tokens
            i += 1
            if i < len(tokenized_lines):
                tokens = tokenized_lines[i]
                accessory_lines = []
                while i < len(tokenized_lines) and main_line is not None:
                    tokens = tokenized_lines[i]
                    if tokens[0] in accessories:
                        accessory_lines.append(tokens)
                    elif tokens[0] == main:  # finished, start new one
                        if len(accessory_lines) > 0:
                            lines_with_accessories["-".join([str(k) for k in main_line[1:3]])] = accessory_lines
                        main_line = None
                    i += 1
        return lines_with_accessories


# ##################################
#
# Entity Helper Classes
#
@dataclass
class ActiveEdge:
    """
    Identifies an edge as in a runway active zone.
    """
    zone: str  # departure, arrival, ils
    runways: str  # up to 4 runways

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'ActiveEdge':
        return ActiveEdge(zone=tokens[1], runways=tokens[2])

    def runway_list(self) -> List[str]:
        return self.runways.split(",")


@dataclass
class RoadNode:
    """
    A node in a road network.
    Every node must be part of one or more edges.
    """
    id: int     # The node identifier (must be unique within an airport)
    lon: float  # Node's longitude
    lat: float  # Node's latitude


@dataclass
class RoadEdge:
    """
    An edge in a road network.
    Every edge is defined by its two node endpoints.
    Edges may support one- or two-way traffic.
    """
    node_begin: int  # The identifier of the beginning node
    node_end: int    # The identifier of the terminal node
    name: str        # The road identifier, may be the empty string
    one_way: bool = False  # If false, it supports two-way traffic

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'RoadEdge':
        name = " ".join(tokens[4:]) if len(tokens) > 4 else ""
        edge = RoadEdge(name=name, node_begin=int(tokens[1]), node_end=int(tokens[2]), one_way=tokens[3] == 'oneway')
        return edge


@dataclass
class RoadNetwork:
    nodes: Dict[int, RoadNode] = field(default_factory=dict)
    edges: List[RoadEdge] = field(default_factory=list)

    @staticmethod
    def from_lines(apt_dat_lines: Collection[AptDatLine]) -> 'RoadNetwork':
        return RoadNetwork.from_tokenized_lines([line.tokens for line in apt_dat_lines if not line.is_ignorable()])

    @staticmethod
    def from_tokenized_lines(tokenized_lines: Collection[List[Union[RowCode, str]]]) -> 'RoadNetwork':
        nodes = {
            node.id: node
            for node in map(lambda tokens: RoadNode(id=int(tokens[4]), lon=float(tokens[2]), lat=float(tokens[1])),
                            filter(lambda line: line[0] == RowCode.TAXI_ROUTE_NODE, tokenized_lines))
        }
        edges = [RoadEdge.from_tokenized_line(tokens)
                 for tokens in tokenized_lines
                 if tokens[0] == RowCode.TAXI_ROUTE_ROAD]
        return RoadNetwork(nodes=nodes, edges=edges)


@dataclass
class TruckParking:
    """
    RowCode.TRUCK_PARKING
    """
    id: int     # The node identifier (must be unique within an airport)
    lon: float  # Node's longitude
    lat: float  # Node's latitude
    heading: float  # Heading (true) of the OBJ positioned at this location
    type_str: str  # Type string (baggage_loader, baggage_train, crew_car, crew_ferrari, crew_limo, pushback, fuel_liners, fuel_jets, fuel_props, food, gpu)
    type_len: int  # 0 to 10 if type is baggage_train, 0 if not
    name: str  # Name of parking

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'TruckParking':
        return TruckParking(id=tokens[1], lat=tokens[2], lon=tokens[3], heading=tokens[4], type_str=tokens[5], type_len=tokens[6], name=tokens[7])


@dataclass
class TruckDestination:
    """
    RowCode.TRUCK_DESTINATION
    """
    id: int     # The node identifier (must be unique within an airport)
    lon: float  # Node's longitude
    lat: float  # Node's latitude
    heading: float  # Heading (true) of the OBJ positioned at this location
    types_str: str  # Truck types allowed to end up at this destination. Pipe separated list.
    name: str  # Name of destination

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'TruckDestination':
        name = " ".join(tokens[6:])
        return TruckDestination(id=tokens[1], lat=tokens[2], lon=tokens[3], heading=tokens[4], types_str=tokens[5], name=name)


@dataclass
class StartupLocation:
    """
    RowCode.START_LOCATION_NEW, START_LOCATION_EXT
    """
    id: int     # The node identifier (must be unique within an airport)
    lon: float  # Node's longitude
    lat: float  # Node's latitude
    heading: float  # Heading (true) of the OBJ positioned at this location
    type_str: str  # Type of location (gate, hangar, misc or tie-down)
    acfs: List[str]  # Airplane types that can use this location. Pipe separated list.
    name: str  # Unique name of location
    icao_code: IcaoWidth  # ICAO width code
    oper_type: str  # Operation types (none, general_aviation, airline, cargo, military)
    airline: str  # Airline permitted to use this ramp. 3-letter airline codes (AAL, SWA, etc)

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]], accessories: Dict[List[Union[RowCode, str]], List[List[Union[RowCode, str]]]]) -> 'StartupLocation':
        icao_code = None
        oper_type = "none"
        airline = ""
        k = f"{tokens[1]}-{tokens[2]}"
        if k in accessories:
            v = accessories[k][0]
            icao_code = IcaoWidth(v[1].upper()) if v[1].upper() in [l.value for l in IcaoWidth] else None
            if len(v) > 2:
                oper_type = v[2]
            if len(v) > 3:
                airline = v[3]
        td = StartupLocation(id=tokens[6], lat=tokens[1], lon=tokens[2], heading=tokens[3], type_str=tokens[4], acfs=tokens[5].split("|"), name=tokens[6], icao_code=icao_code, oper_type=oper_type, airline=airline)
        return td


@dataclass
class DetailedAirport(Airport):

    @staticmethod
    def from_lines(dat_lines: List[str], from_file_name: Optional[Path] = None, xplane_version: int = 1100) -> 'Airport':
        """
        :param dat_lines: The lines of the apt.dat file
        :param from_file_name: The name of the apt.dat file you read this airport in from
        :param xplane_version: The version of the apt.dat spec this airport uses (1050, 1100, 1130, etc.)
        """
        tokenized = [AptDatLine.tokenize(line) for line in dat_lines if line.lstrip()]
        return DetailedAirport(from_file_name, dat_lines, xplane_version, tokenized)

    @staticmethod
    def from_str(file_text: str, from_file_name: Optional[PathLike] = None, xplane_version: int = 1100) -> 'Airport':
        """
        :param file_text: The portion of the apt.dat file text that specifies this airport
        :param from_file_name: The name of the apt.dat file you read this airport in from
        """
        cleaned_lines = list(filter(lambda l: not AptDatLine.raw_is_ignorable(l), file_text.splitlines()))
        return DetailedAirport.from_lines(cleaned_lines, from_file_name, xplane_version)

    @apt_cached_property
    def road_network(self) -> RoadNetwork:
        return RoadNetwork.from_tokenized_lines(self.tokenized_lines)

    @apt_cached_property
    def truck_parkings(self) -> List[TruckParking]:
        return [TruckParking.from_tokenized_line(tokens=t) for t in self.tokenized_lines if t[0] == RowCode.TRUCK_PARKING]

    @apt_cached_property
    def truck_destinations(self) -> List[TruckDestination]:
        return [TruckDestination.from_tokenized_line(tokens=t) for t in self.tokenized_lines if t[0] == RowCode.TRUCK_DESTINATION]

    @apt_cached_property
    def startup_locations(self) -> List[StartupLocation]:
        a = Accessories.from_tokenized_lines(tokenized_lines=self.tokenized_lines, main=RowCode.START_LOCATION_NEW, accessories=[RowCode.START_LOCATION_EXT])
        return [StartupLocation.from_tokenized_line(tokens=t, accessories=a) for t in self.tokenized_lines if t[0] == RowCode.START_LOCATION_NEW]

    def inject_active_zones(self):
        a = Accessories.from_tokenized_lines(tokenized_lines=self.tokenized_lines, main=RowCode.TAXI_ROUTE_EDGE, accessories=[RowCode.TAXI_ROUTE_HOLD])
        for e in self.taxi_network.edges:
            k = f"{e.node_begin}-{e.node_end}"
            e.active_zones = [ActiveEdge(zone=t[1], runways=t[2]) for t in a[k]] if k in a else None
