"""
Tools for reading, inspecting, and manipulating X-Plane’s airport (apt.dat) files.
"""
from dataclasses import dataclass, field
from functools import reduce
from operator import add
from os import PathLike
from pathlib import Path
from typing import Collection, Dict, List, Optional, Union
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
    def key(tokens: List[Union[RowCode, str]], key: List[int] = [1, 3], sep: str = "-") -> str:
        return sep.join([str(k) for k in tokens[min(key[0], len(tokens)):min(key[1], len(tokens))]])

    @staticmethod
    def from_tokenized_lines(tokenized_lines: List[List[Union[RowCode, str]]], main: RowCode, accessories: List[RowCode], key: List[int] = [1, 3], sep: str = "-") -> Dict[List[Union[RowCode, str]], List[List[Union[RowCode, str]]]]:
        lines_with_accessories = {}
        main_line = None
        accessory_lines = []

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
                        k = Accessories.key(tokens=main_line, key=key, sep=sep)
                        if len(accessory_lines) > 0:
                            lines_with_accessories[k] = accessory_lines
                            accessory_lines = []
                            main_line = tokens
                    i += 1

        if len(accessory_lines) > 0: # push last one if any
            k = Accessories.key(tokens=main_line, key=key, sep=sep)
            lines_with_accessories[k] = accessory_lines

        return lines_with_accessories


# ######################################################
#
# E N T I T Y   H E L P E R   C L A S S E S
#
# ######################################################
#
# Taxi and road networks
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
    lon: float  # Node's longitude
    lat: float  # Node's latitude
    heading: float  # Heading (true) of the OBJ positioned at this location
    type_str: str  # Type string (baggage_loader, baggage_train, crew_car, crew_ferrari, crew_limo, pushback, fuel_liners, fuel_jets, fuel_props, food, gpu)
    type_len: int  # 0 to 10 if type is baggage_train, 0 if not
    name: str  # Name of parking

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'TruckParking':
        name = " ".join(tokens[6:]) if len(tokens) > 6 else ""
        return TruckParking(lat=float(tokens[1]), lon=float(tokens[2]), heading=float(tokens[3]), type_str=tokens[4], type_len=int(tokens[5]), name=name)


@dataclass
class TruckDestination:
    """
    RowCode.TRUCK_DESTINATION
    """
    lon: float  # Node's longitude
    lat: float  # Node's latitude
    heading: float  # Heading (true) of the OBJ positioned at this location
    types_str: list  # Truck types allowed to end up at this destination. Pipe separated list.
    name: str  # Name of destination

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'TruckDestination':
        name = " ".join(tokens[5:]) if len(tokens) > 5 else ""
        return TruckDestination(lat=float(tokens[1]), lon=float(tokens[2]), heading=float(tokens[3]), types_str=tokens[4].split("|"), name=name)


@dataclass
class StartupLocation:
    """
    RowCode.START_LOCATION_NEW, START_LOCATION_EXT
    """
    lon: float  # Node's longitude
    lat: float  # Node's latitude
    heading: float  # Heading (true) of the OBJ positioned at this location
    type_str: str  # Type of location (gate, hangar, misc or tie-down)
    aircraft_types: List[str]  # Airplane types that can use this location. Pipe separated list.
    name: str  # Unique name of location
    icao_code: IcaoWidth  # ICAO width code
    oper_type: str  # Operation types (none, general_aviation, airline, cargo, military)
    airline: str  # Airline permitted to use this ramp. 3-letter airline codes (AAL, SWA, etc)

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]], accessories: Dict[List[Union[RowCode, str]], List[List[Union[RowCode, str]]]]) -> 'StartupLocation':
        icao_code = None
        oper_type = "none"
        airline = ""
        k = Accessories.key(tokens=tokens)
        if k in accessories:
            v = accessories[k][0]
            icao_code = IcaoWidth(v[1].upper()) if v[1].upper() in [l.value for l in IcaoWidth] else None
            if len(v) > 2:
                oper_type = v[2]
            if len(v) > 3:
                airline = v[3]
        td = StartupLocation(lat=float(tokens[1]), lon=float(tokens[2]), heading=float(tokens[3]), type_str=tokens[4], aircraft_types=tokens[5].split("|"), name=tokens[6], icao_code=icao_code, oper_type=oper_type, airline=airline)
        return td


# ######################################################
#
# Runways
#
@dataclass
class Runway:
    """
    RowCode.*_RUNWAY
    """
    name: str  # Name of runway
    lon: float  # Runway end's longitude
    lat: float  # Runway end's latitude
    width: float  # Runway width in meter


@dataclass
class RunwayLand(Runway):
    """
    RowCode.LAND_RUNWAY
    ```
    #   0     1 2 3    4 5 6 7    8            9               10 11  1213141516   17           18              19 20  21222324
    100 60.00 1 1 0.25 1 3 0 16L  25.29609337  051.60889908    0  300 2 2 1 0 34R  25.25546269  051.62677745    0  306 3 2 1 0
    ```
    """
    surface_type: int
    shoulder_type: int
    smoothness: float

    center_lights: int
    edge_lights: int
    distance_remaining: int

    end_lon: float  # Runway end's longitude
    end_lat: float  # Runway end's latitude

    threshold: float  # Length of displaced threshold in metres (this is included in implied runway length)A displaced threshold will always be inside (between) the two runway ends
    overrun: float  # Length of overrun/blast-pad in metres (not included in implied runway length)
    marking: int  # Code for runway markings (Visual, non-precision, precision)
    approach_lighting: int  # Code for approach lighting for this runway end
    touch_down: int  # Flag for runway touchdown zone (TDZ) lighting
    runway_end: int  # Code for Runway End Identifier Lights (REIL)

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> List['RunwayLand']:
        r1 = RunwayLand(name=tokens[8],
                    width=float(tokens[1]),
                    lon=float(tokens[10]),
                    lat=float(tokens[9]),
                    surface_type=int(tokens[2]),
                    shoulder_type=int(tokens[3]),
                    smoothness=float(tokens[4]),
                    center_lights=int(tokens[5]),
                    edge_lights=int(tokens[6]),
                    distance_remaining=int(tokens[7]),
                    end_lon=float(tokens[19]),
                    end_lat=float(tokens[18]),
                    threshold=float(tokens[11]),
                    overrun=float(tokens[12]),
                    marking=int(tokens[13]),
                    approach_lighting=int(tokens[14]),
                    touch_down=int(tokens[15]),
                    runway_end=int(tokens[16]),
                )
        if len(tokens) <20:  # only one runway direction
            return [ r1 ]
        r2 = RunwayLand(name=tokens[17],
                    width=float(tokens[1]),
                    lon=float(tokens[19]),
                    lat=float(tokens[18]),
                    surface_type=int(tokens[2]),
                    shoulder_type=int(tokens[3]),
                    smoothness=float(tokens[4]),
                    center_lights=int(tokens[5]),
                    edge_lights=int(tokens[6]),
                    distance_remaining=int(tokens[7]),
                    end_lon=float(tokens[10]),
                    end_lat=float(tokens[9]),
                    threshold=float(tokens[20]),
                    overrun=float(tokens[21]),
                    marking=int(tokens[22]),
                    approach_lighting=int(tokens[23]),
                    touch_down=int(tokens[24]),
                    runway_end=int(tokens[25]),
                )
        return [r1, r2]


@dataclass
class RunwayWater(Runway):
    """
    RowCode.LAND_RUNWAY
    """
    buoys: int  # Flag for perimeter buoys, 0=no buoys, 1=render buoys

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'RunwayWater':
        return RunwayWater(name=tokens[3],
            width=float(tokens[1]),
            buoys=int(tokens[2]),
            lon=float(tokens[5]),
            lat=float(tokens[4]),
        )


@dataclass
class Helipad(Runway):
    """
    RowCode.LAND_RUNWAY
    """
    orientation: float
    length: float
    width: float
    surface_code: int
    marking: int
    smoothness: float
    edge_lighting: int

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'Helipad':
        return Helipad(name=tokens[1],
            lon=float(tokens[3]),
            lat=float(tokens[2]),
            orientation=float(tokens[4]),
            length=float(tokens[5]),
            width=float(tokens[6]),
            surface_code=int(tokens[7]),
            marking=int(tokens[8]),
            smoothness=float(tokens[9]),
            edge_lighting=int(tokens[10]),
        )


# ######################################################
#
# Airport traffic flow
#
@dataclass
class AirportFlow:
    """
    RowCode.FLOW_*
    """
    name: str  # Airport flow name

    @staticmethod
    def from_tokenized_line(tokens: List[Union[RowCode, str]]) -> 'AirportFlow':
        return AirportFlow(name=" ".join(tokens[1:]))


@dataclass
class AFWind(AirportFlow):
    """
    RowCode.1001
    """
    station: str  # METAR reporting station (may be a remote airport)
    wind_dir_min: float  # Wind direction minimum (magnetic) (int?)
    wind_dir_max: float  # Wind direction maximum (magnetic)
    wind_speed_max: float = 999  # Use 999 for ‘all’ wind speeds. (unit?)

    @staticmethod
    def from_tokenized_line(name: str, tokens: List[Union[RowCode, str]]) -> 'AFWind':
        return AFWind(name=name, station=tokens[1], wind_dir_min=float(tokens[2]), wind_dir_max=float(tokens[3]), wind_speed_max=float(tokens[4]))


@dataclass
class AFCeiling(AirportFlow):
    """
    RowCode.1002
    """
    station: str  # METAR reporting station (may be a remote airport)
    ceiling_ft: int  # Minimum reported ceiling in feet AGL at reporting station

    @staticmethod
    def from_tokenized_line(name: str, tokens: List[Union[RowCode, str]]) -> 'AFCeiling':
        return AFCeiling(name=name, station=tokens[1], ceiling_ft=int(tokens[2]))


@dataclass
class AFVisibility(AirportFlow):
    """
    RowCode.1003
    """
    station: str  # METAR reporting station (may be a remote airport)
    visibility_sm: float  # Minimum reported visibility in statute miles

    @staticmethod
    def from_tokenized_line(name: str, tokens: List[Union[RowCode, str]]) -> 'AFVisibility':
        return AFVisibility(name=name, station=tokens[1], visibility_sm=float(tokens[2]))


@dataclass
class AFTime(AirportFlow):
    """
    RowCode.1004
    """
    utc_start: str  # UTC time from which rule is valid (0000 - 2400)
    utc_end: str

    @staticmethod
    def from_tokenized_line(name: str, tokens: List[Union[RowCode, str]]) -> 'AFTime':
        return AFTime(name=name, utc_start=tokens[1], utc_end=tokens[2])


# ######################################################
#
# Runways In Use
#
@dataclass
class RunwayInUse(AirportFlow):
    """
    RowCode.1100, 1110
    """
    rule_name: str  # Rule name
    runway: str  # Runway end identifier
    frequency: float  # Arrival or departure frequency
    rule_type: list  # arrivals|departures
    jets: list  # Airplane types to which rule applies
    on_course: str  # On course heading range ((ie. first leg of flight plan  for departures, last leg for arrivals, 000000 – 359359)
    initial: str  # Initial ATC assigned departure heading range.  Not used for arrivals. (000000 – 359359)

    @staticmethod
    def from_tokenized_line(name: str, tokens: List[Union[RowCode, str]]) -> 'RunwayInUse':
        f = int(tokens[2]) / (1000 if len(tokens[2]) > 5 else 100)
        return RunwayInUse(name=name, rule_name=" ".join(tokens[7:]), runway=tokens[1], frequency=f, rule_type=tokens[3], jets=tokens[4].split("|"), on_course=tokens[5], initial=tokens[6])


@dataclass
class VFRPattern(AirportFlow):
    """
    RowCode.1100, 1110
    """
    runway: str  # Runway end identifier
    pattern: str  # VFR traffic pattern direction

    @staticmethod
    def from_tokenized_line(name: str, tokens: List[Union[RowCode, str]]) -> 'VFRPattern':
        return VFRPattern(name=name, runway=tokens[1], pattern=tokens[2])


AIRPORT_FLOWS = {
    RowCode.FLOW_WIND: AFWind,
    RowCode.FLOW_CEILING: AFCeiling,
    RowCode.FLOW_TIME: AFTime,
    RowCode.FLOW_VISIBILITY: AFVisibility,
    RowCode.FLOW_RUNWAY_RULE: RunwayInUse,
    RowCode.FLOW_RUNWAY_RULE_CHANNEL: RunwayInUse,
    RowCode.FLOW_PATTERN: VFRPattern
}

# ######################################################
#
# D E T A I L E D   A I R P O R T   E N T I T Y
#
@dataclass
class DetailedAirport(Airport):
    """Complement Airport class to provide additional details.

    Complement Airport class to provide attributes such as:
    - Road network
    - Truck Parkings
    - Truck Destinations
    - Startup Locations
    - Runways (land, water, helipads)
    together with meta data.

    This allows for better airport management where geometries, etc. are needed.
    For example to build routes to destination for cars, aircrafts, etc.
    """
    @staticmethod
    def from_airport(airport: Airport) -> 'DetailedAirport':
        """
        :param dat_lines: The lines of the apt.dat file
        :param from_file_name: The name of the apt.dat file you read this airport in from
        :param xplane_version: The version of the apt.dat spec this airport uses (1050, 1100, 1130, etc.)
        """
        d = DetailedAirport.from_str(file_text="\n".join(airport.raw_lines))
        d._inject_active_zones()
        return d

    @staticmethod
    def from_lines(dat_lines: List[str], from_file_name: Optional[Path] = None, xplane_version: int = 1100) -> 'DetailedAirport':
        """
        :param dat_lines: The lines of the apt.dat file
        :param from_file_name: The name of the apt.dat file you read this airport in from
        :param xplane_version: The version of the apt.dat spec this airport uses (1050, 1100, 1130, etc.)
        """
        tokenized = [AptDatLine.tokenize(line) for line in dat_lines if line.lstrip()]
        return DetailedAirport(from_file_name, dat_lines, xplane_version, tokenized)

    @staticmethod
    def from_str(file_text: str, from_file_name: Optional[PathLike] = None, xplane_version: int = 1100) -> 'DetailedAirport':
        """
        :param file_text: The portion of the apt.dat file text that specifies this airport
        :param from_file_name: The name of the apt.dat file you read this airport in from
        """
        cleaned_lines = list(filter(lambda l: not AptDatLine.raw_is_ignorable(l), file_text.splitlines()))
        return DetailedAirport.from_lines(cleaned_lines, from_file_name, xplane_version)

    def _inject_active_zones(self):
        a = Accessories.from_tokenized_lines(tokenized_lines=self.tokenized_lines, main=RowCode.TAXI_ROUTE_EDGE, accessories=[RowCode.TAXI_ROUTE_HOLD])
        for e in self.taxi_network.edges:
            k = f"{e.node_begin}-{e.node_end}"
            e.active_zones = [ActiveEdge(zone=t[1], runways=t[2]) for t in a[k]] if k in a else None

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

    @apt_cached_property
    def land_runways(self) -> List[RunwayLand]:
        return reduce(add, [RunwayLand.from_tokenized_line(tokens=t) for t in self.tokenized_lines if t[0] == RowCode.LAND_RUNWAY])

    @apt_cached_property
    def water_runways(self) -> List[RunwayWater]:
        return [RunwayWater.from_tokenized_line(tokens=t) for t in self.tokenized_lines if t[0] == RowCode.WATER_RUNWAY]

    @apt_cached_property
    def helipads(self) -> List[Helipad]:
        return [Helipad.from_tokenized_line(tokens=t) for t in self.tokenized_lines if t[0] == RowCode.HELIPAD]

    @apt_cached_property
    def airport_flows(self) -> List[AirportFlow]:
        a = Accessories.from_tokenized_lines(tokenized_lines=self.tokenized_lines, main=RowCode.FLOW_DEFINITION, accessories=AIRPORT_FLOWS.keys(), key=[1, 10], sep=" ")
        flows = []
        for k, acc in a.items():
            flows += [AIRPORT_FLOWS.get(v[0]).from_tokenized_line(name=k, tokens=v) for v in acc]
        return flows

#