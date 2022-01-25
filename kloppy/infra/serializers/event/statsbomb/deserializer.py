from typing import Tuple, Dict, List, NamedTuple, IO
import logging
import json

from kloppy.domain import (
    EventDataset,
    Team,
    Period,
    Point,
    BallState,
    DatasetFlag,
    Orientation,
    Provider,
    PassEvent,
    ShotEvent,
    TakeOnEvent,
    CarryEvent,
    GenericEvent,
    PassResult,
    ShotResult,
    TakeOnResult,
    CarryResult,
    EventType,
    Metadata,
    Ground,
    Player,
    SubstitutionEvent,
    CardEvent,
    PlayerOnEvent,
    PlayerOffEvent,
    CardType,
    Qualifier,
    SetPieceQualifier,
    SetPieceType,
    RecoveryEvent,
    FoulCommittedEvent,
    BallOutEvent,
    BodyPart,
    BodyPartQualifier,
)
from kloppy.exceptions import DeserializationError
from kloppy.utils import performance_logging

from ..deserializer import EventDataDeserializer

logger = logging.getLogger(__name__)

SB_EVENT_TYPE_RECOVERY = 2
SB_EVENT_TYPE_DRIBBLE = 14
SB_EVENT_TYPE_SHOT = 16
SB_EVENT_TYPE_PASS = 30
SB_EVENT_TYPE_CARRY = 43

SB_EVENT_TYPE_HALF_START = 18
SB_EVENT_TYPE_HALF_END = 34
SB_EVENT_TYPE_STARTING_XI = 35

SB_EVENT_TYPE_SUBSTITUTION = 19
SB_EVENT_TYPE_FOUL_COMMITTED = 22
SB_EVENT_TYPE_BAD_BEHAVIOUR = 24
SB_EVENT_TYPE_PLAYER_ON = 26
SB_EVENT_TYPE_PLAYER_OFF = 27

SB_PASS_OUTCOME_COMPLETE = 8
SB_PASS_OUTCOME_INCOMPLETE = 9
SB_PASS_OUTCOME_INJURY_CLEARANCE = 74
SB_PASS_OUTCOME_OUT = 75
SB_PASS_OUTCOME_OFFSIDE = 76
SB_PASS_OUTCOME_UNKNOWN = 77

SB_SHOT_OUTCOME_BLOCKED = 96
SB_SHOT_OUTCOME_GOAL = 97
SB_SHOT_OUTCOME_OFF_TARGET = 98
SB_SHOT_OUTCOME_POST = 99
SB_SHOT_OUTCOME_SAVED = 100
SB_SHOT_OUTCOME_OFF_WAYWARD = 101
SB_SHOT_OUTCOME_SAVED_OFF_TARGET = 115
SB_SHOT_OUTCOME_SAVED_TO_POST = 116

SB_EVENT_TYPE_FREE_KICK = 62
SB_EVENT_TYPE_THROW_IN = 67
SB_EVENT_TYPE_KICK_OFF = 65
SB_EVENT_TYPE_CORNER_KICK = 61
SB_EVENT_TYPE_PENALTY = 88
SB_EVENT_TYPE_GOAL_KICK = 63

OUT_EVENT_RESULTS = [PassResult.OUT, TakeOnResult.OUT]

SB_BODYPART_BOTH_HANDS = 35
SB_BODYPART_CHEST = 36
SB_BODYPART_HEAD = 37
SB_BODYPART_LEFT_FOOT = 38
SB_BODYPART_LEFT_HAND = 39
SB_BODYPART_RIGHT_FOOT = 40
SB_BODYPART_RIGHT_HAND = 41
SB_BODYPART_DROP_KICK = 68
SB_BODYPART_KEEPER_ARM = 69
SB_BODYPART_OTHER = 70
SB_BODYPART_NO_TOUCH = 106


def parse_str_ts(timestamp: str) -> float:
    h, m, s = timestamp.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _parse_coordinates(
    coordinates: List[float], fidelity_version: int
) -> Point:
    # location is cell based
    # [1, 120] x [1, 80]
    # +-----+------+
    # | 1,1 | 2, 1 |
    # +-----+------+
    # | 1,2 | 2,2  |
    # +-----+------+
    cell_side = 0.1 if fidelity_version == 2 else 1.0
    cell_relative_center = cell_side / 2
    return Point(
        x=coordinates[0] - cell_relative_center,
        y=coordinates[1] - cell_relative_center,
    )


def _parse_bodypart(event_dict: Dict) -> BodyPart:
    bodypart_id = event_dict["body_part"]["id"]
    if bodypart_id == SB_BODYPART_BOTH_HANDS:
        body_part = BodyPart.BOTH_HANDS
    elif bodypart_id == SB_BODYPART_CHEST:
        body_part = BodyPart.CHEST
    elif bodypart_id == SB_BODYPART_HEAD:
        body_part = BodyPart.HEAD
    elif bodypart_id == SB_BODYPART_LEFT_FOOT:
        body_part = BodyPart.LEFT_FOOT
    elif bodypart_id == SB_BODYPART_LEFT_HAND:
        body_part = BodyPart.LEFT_HAND
    elif bodypart_id == SB_BODYPART_RIGHT_FOOT:
        body_part = BodyPart.RIGHT_FOOT
    elif bodypart_id == SB_BODYPART_RIGHT_HAND:
        body_part = BodyPart.RIGHT_HAND
    elif bodypart_id == SB_BODYPART_DROP_KICK:
        body_part = BodyPart.DROP_KICK
    elif bodypart_id == SB_BODYPART_KEEPER_ARM:
        body_part = BodyPart.KEEPER_ARM
    elif bodypart_id == SB_BODYPART_OTHER:
        body_part = BodyPart.OTHER
    elif bodypart_id == SB_BODYPART_NO_TOUCH:
        body_part = BodyPart.NO_TOUCH
    else:
        raise DeserializationError(f"Unknown body part: {bodypart_id}")

    return body_part


def _parse_pass(pass_dict: Dict, team: Team, fidelity_version: int) -> Dict:
    if "outcome" in pass_dict:
        outcome_id = pass_dict["outcome"]["id"]
        if outcome_id == SB_PASS_OUTCOME_OUT:
            result = PassResult.OUT
        elif outcome_id == SB_PASS_OUTCOME_INCOMPLETE:
            result = PassResult.INCOMPLETE
        elif outcome_id == SB_PASS_OUTCOME_OFFSIDE:
            result = PassResult.OFFSIDE
        elif outcome_id == SB_PASS_OUTCOME_INJURY_CLEARANCE:
            result = PassResult.OUT
        elif outcome_id == SB_PASS_OUTCOME_UNKNOWN:
            result = None
        else:
            raise DeserializationError(f"Unknown pass outcome: {outcome_id}")

        receiver_player = None
    else:
        result = PassResult.COMPLETE
        receiver_player = team.get_player_by_id(pass_dict["recipient"]["id"])

    receiver_coordinates = _parse_coordinates(
        pass_dict["end_location"],
        fidelity_version,
    )

    qualifiers = _get_event_qualifiers(pass_dict)

    return {
        "result": result,
        "receiver_coordinates": receiver_coordinates,
        "receiver_player": receiver_player,
        "qualifiers": qualifiers,
    }


def _get_event_qualifiers(qualifiers_dict: Dict) -> List[Qualifier]:
    qualifiers = []
    if "type" in qualifiers_dict:
        if qualifiers_dict["type"]["id"] == SB_EVENT_TYPE_CORNER_KICK:
            qualifiers.append(
                SetPieceQualifier(value=SetPieceType.CORNER_KICK)
            )
        elif qualifiers_dict["type"]["id"] == SB_EVENT_TYPE_FREE_KICK:
            qualifiers.append(SetPieceQualifier(value=SetPieceType.FREE_KICK))
        elif qualifiers_dict["type"]["id"] == SB_EVENT_TYPE_PENALTY:
            qualifiers.append(SetPieceQualifier(value=SetPieceType.PENALTY))
        elif qualifiers_dict["type"]["id"] == SB_EVENT_TYPE_THROW_IN:
            qualifiers.append(SetPieceQualifier(value=SetPieceType.THROW_IN))
        elif qualifiers_dict["type"]["id"] == SB_EVENT_TYPE_KICK_OFF:
            qualifiers.append(SetPieceQualifier(value=SetPieceType.KICK_OFF))
        elif qualifiers_dict["type"]["id"] == SB_EVENT_TYPE_GOAL_KICK:
            qualifiers.append(SetPieceQualifier(value=SetPieceType.GOAL_KICK))

    if "body_part" in qualifiers_dict:
        qualifiers.append(BodyPartQualifier(_parse_bodypart(qualifiers_dict)))

    return qualifiers


def _parse_shot(shot_dict: Dict) -> Dict:
    outcome_id = shot_dict["outcome"]["id"]
    if outcome_id == SB_SHOT_OUTCOME_OFF_TARGET:
        result = ShotResult.OFF_TARGET
    elif outcome_id == SB_SHOT_OUTCOME_SAVED:
        result = ShotResult.SAVED
    elif outcome_id == SB_SHOT_OUTCOME_SAVED_OFF_TARGET:
        result = ShotResult.SAVED
    elif outcome_id == SB_SHOT_OUTCOME_SAVED_TO_POST:
        result = ShotResult.SAVED
    elif outcome_id == SB_SHOT_OUTCOME_POST:
        result = ShotResult.POST
    elif outcome_id == SB_SHOT_OUTCOME_OFF_WAYWARD:
        result = ShotResult.OFF_TARGET
    elif outcome_id == SB_SHOT_OUTCOME_BLOCKED:
        result = ShotResult.BLOCKED
    elif outcome_id == SB_SHOT_OUTCOME_GOAL:
        result = ShotResult.GOAL
    else:
        raise DeserializationError(f"Unknown shot outcome: {outcome_id}")

    qualifiers = _get_event_qualifiers(shot_dict)

    return {
        "result": result,
        "qualifiers": qualifiers,
    }


def _parse_carry(carry_dict: Dict, fidelity_version: int) -> Dict:
    return {
        "result": CarryResult.COMPLETE,
        "end_coordinates": _parse_coordinates(
            carry_dict["end_location"],
            fidelity_version,
        ),
    }


def _parse_take_on(take_on_dict: Dict) -> Dict:
    if "outcome" in take_on_dict:
        outcome_id = take_on_dict["outcome"]["id"]
        if outcome_id == SB_PASS_OUTCOME_OUT:
            result = TakeOnResult.OUT
        elif outcome_id == SB_PASS_OUTCOME_INCOMPLETE:
            result = TakeOnResult.INCOMPLETE
        elif outcome_id == SB_PASS_OUTCOME_COMPLETE:
            result = TakeOnResult.COMPLETE
        else:
            raise DeserializationError(
                f"Unknown pass outcome: {take_on_dict['outcome']['name']}({outcome_id})"
            )
    else:
        result = TakeOnResult.COMPLETE

    return {
        "result": result,
    }


def _parse_substitution(substitution_dict: Dict, team: Team) -> Dict:
    replacement_player = None
    for player in team.players:
        if player.player_id == str(substitution_dict["replacement"]["id"]):
            replacement_player = player
            break
    else:
        raise DeserializationError(
            f'Could not find replacement player {substitution_dict["replacement"]["id"]}'
        )

    return {
        "replacement_player": replacement_player,
    }


def _parse_bad_behaviour(bad_behaviour_dict: Dict) -> Dict:
    bad_behaviour = {}
    if "card" in bad_behaviour_dict:
        bad_behaviour["card"] = _parse_card(bad_behaviour_dict["card"])

    return bad_behaviour


def _parse_foul_committed(foul_committed_dict: Dict) -> Dict:
    foul_committed = {}
    if "card" in foul_committed_dict:
        foul_committed["card"] = _parse_card(foul_committed_dict["card"])

    return foul_committed


def _parse_card(card_dict: Dict) -> Dict:
    card_id = card_dict["id"]
    if card_id in (5, 65):
        card_type = CardType.RED
    elif card_id in (6, 66):
        card_type = CardType.SECOND_YELLOW
    elif card_id in (7, 67):
        card_type = CardType.FIRST_YELLOW
    else:
        raise DeserializationError(f"Unknown card id {card_id}")

    return {
        "card_type": card_type,
    }


def _determine_xy_fidelity_versions(events: List[Dict]) -> Tuple[int, int]:
    """
    Find out if x and y are integers disguised as floats
    """
    shot_fidelity_version = 1
    xy_fidelity_version = 1
    for event in events:
        if "location" in event:
            x, y, *_ = event["location"]

            if not x.is_integer() or not y.is_integer():
                event_type = event["type"]["id"]
                if event_type == SB_EVENT_TYPE_SHOT:
                    shot_fidelity_version = 2
                elif event_type in (
                    SB_EVENT_TYPE_CARRY,
                    SB_EVENT_TYPE_DRIBBLE,
                    SB_EVENT_TYPE_PASS,
                ):
                    xy_fidelity_version = 2
    return shot_fidelity_version, xy_fidelity_version


class StatsbombInputs(NamedTuple):
    event_data: IO[bytes]
    lineup_data: IO[bytes]


class StatsBombDeserializer(EventDataDeserializer[StatsbombInputs]):
    @property
    def provider(self) -> Provider:
        return Provider.STATSBOMB

    def deserialize(self, inputs: StatsbombInputs) -> EventDataset:
        transformer = self.get_transformer(length=120, width=80)

        with performance_logging("load data", logger=logger):
            raw_events = json.load(inputs.event_data)
            home_lineup, away_lineup = json.load(inputs.lineup_data)
            (
                shot_fidelity_version,
                xy_fidelity_version,
            ) = _determine_xy_fidelity_versions(raw_events)
            logger.info(
                f"Determined Fidelity versions: shot v{shot_fidelity_version} / XY v{xy_fidelity_version}"
            )

        with performance_logging("parse data", logger=logger):
            starting_player_ids = {
                str(player["player"]["id"])
                for raw_event in raw_events
                if raw_event["type"]["id"] == SB_EVENT_TYPE_STARTING_XI
                for player in raw_event["tactics"]["lineup"]
            }
            home_team = Team(
                team_id=str(home_lineup["team_id"]),
                name=home_lineup["team_name"],
                ground=Ground.HOME,
            )
            home_team.players = [
                Player(
                    player_id=str(player["player_id"]),
                    team=home_team,
                    name=player["player_name"],
                    jersey_no=int(player["jersey_number"]),
                    starting=str(player["player_id"]) in starting_player_ids,
                )
                for player in home_lineup["lineup"]
            ]

            away_team = Team(
                team_id=str(away_lineup["team_id"]),
                name=away_lineup["team_name"],
                ground=Ground.AWAY,
            )
            away_team.players = [
                Player(
                    player_id=str(player["player_id"]),
                    team=away_team,
                    name=player["player_name"],
                    jersey_no=int(player["jersey_number"]),
                    starting=str(player["player_id"]) in starting_player_ids,
                )
                for player in away_lineup["lineup"]
            ]

            teams = [home_team, away_team]

            periods = []
            period = None
            events = []
            for raw_event in raw_events:
                if raw_event["team"]["id"] == home_lineup["team_id"]:
                    team = home_team
                elif raw_event["team"]["id"] == away_lineup["team_id"]:
                    team = away_team
                else:
                    raise DeserializationError(
                        f"Unknown team_id {raw_event['team']['id']}"
                    )

                if (
                    raw_event["possession_team"]["id"]
                    == home_lineup["team_id"]
                ):
                    possession_team = home_team
                elif (
                    raw_event["possession_team"]["id"]
                    == away_lineup["team_id"]
                ):
                    possession_team = away_team
                else:
                    raise DeserializationError(
                        f"Unknown possession_team_id: {raw_event['possession_team']}"
                    )

                timestamp = parse_str_ts(raw_event["timestamp"])
                period_id = int(raw_event["period"])
                if not period or period.id != period_id:
                    period = Period(
                        id=period_id,
                        start_timestamp=(
                            timestamp
                            if not period
                            # period = [start, end], add millisecond to prevent overlapping
                            else timestamp + period.end_timestamp + 0.001
                        ),
                        end_timestamp=None,
                    )
                    periods.append(period)
                else:
                    period.end_timestamp = period.start_timestamp + timestamp

                player = None
                if "player" in raw_event:
                    player = team.get_player_by_id(raw_event["player"]["id"])

                event_type = raw_event["type"]["id"]
                if event_type == SB_EVENT_TYPE_SHOT:
                    fidelity_version = shot_fidelity_version
                elif event_type in (
                    SB_EVENT_TYPE_CARRY,
                    SB_EVENT_TYPE_DRIBBLE,
                    SB_EVENT_TYPE_PASS,
                ):
                    fidelity_version = xy_fidelity_version
                else:
                    # TODO: Uh ohhhh.. don't know which one to pick
                    fidelity_version = xy_fidelity_version

                generic_event_kwargs = {
                    # from DataRecord
                    "period": period,
                    "timestamp": timestamp,
                    "ball_owning_team": possession_team,
                    "ball_state": BallState.ALIVE,
                    # from Event
                    "event_id": raw_event["id"],
                    "team": team,
                    "player": player,
                    "coordinates": (
                        _parse_coordinates(
                            raw_event.get("location"),
                            fidelity_version,
                        )
                        if "location" in raw_event
                        else None
                    ),
                    "raw_event": raw_event,
                }

                new_events = []
                if event_type == SB_EVENT_TYPE_PASS:
                    pass_event_kwargs = _parse_pass(
                        pass_dict=raw_event["pass"],
                        team=team,
                        fidelity_version=fidelity_version,
                    )
                    pass_event = PassEvent.create(
                        # TODO: Consider moving this to _parse_pass
                        receive_timestamp=timestamp + raw_event["duration"],
                        **pass_event_kwargs,
                        **generic_event_kwargs,
                    )
                    new_events.append(pass_event)
                elif event_type == SB_EVENT_TYPE_SHOT:
                    shot_event_kwargs = _parse_shot(
                        shot_dict=raw_event["shot"],
                    )
                    shot_event = ShotEvent.create(
                        **shot_event_kwargs,
                        **generic_event_kwargs,
                    )
                    new_events.append(shot_event)

                # For dribble and carry the definitions
                # are flipped between Statsbomb and kloppy
                elif event_type == SB_EVENT_TYPE_DRIBBLE:
                    take_on_event_kwargs = _parse_take_on(
                        take_on_dict=raw_event["dribble"],
                    )
                    take_on_event = TakeOnEvent.create(
                        qualifiers=None,
                        **take_on_event_kwargs,
                        **generic_event_kwargs,
                    )
                    new_events.append(take_on_event)
                elif event_type == SB_EVENT_TYPE_CARRY:
                    carry_event_kwargs = _parse_carry(
                        carry_dict=raw_event["carry"],
                        fidelity_version=fidelity_version,
                    )
                    carry_event = CarryEvent.create(
                        qualifiers=None,
                        # TODO: Consider moving this to _parse_carry
                        end_timestamp=timestamp + raw_event.get("duration", 0),
                        **carry_event_kwargs,
                        **generic_event_kwargs,
                    )
                    new_events.append(carry_event)

                # lineup affecting events
                elif event_type == SB_EVENT_TYPE_SUBSTITUTION:
                    substitution_event_kwargs = _parse_substitution(
                        substitution_dict=raw_event["substitution"],
                        team=team,
                    )
                    substitution_event = SubstitutionEvent.create(
                        result=None,
                        qualifiers=None,
                        **substitution_event_kwargs,
                        **generic_event_kwargs,
                    )
                    new_events.append(substitution_event)
                elif event_type == SB_EVENT_TYPE_BAD_BEHAVIOUR:
                    bad_behaviour_kwargs = _parse_bad_behaviour(
                        bad_behaviour_dict=raw_event.get("bad_behaviour", {}),
                    )
                    if "card" in bad_behaviour_kwargs:
                        card_kwargs = bad_behaviour_kwargs["card"]
                        card_event = CardEvent.create(
                            result=None,
                            qualifiers=None,
                            card_type=card_kwargs["card_type"],
                            **generic_event_kwargs,
                        )
                        new_events.append(card_event)
                elif event_type == SB_EVENT_TYPE_FOUL_COMMITTED:
                    foul_committed_kwargs = _parse_foul_committed(
                        foul_committed_dict=raw_event.get(
                            "foul_committed", {}
                        ),
                    )
                    foul_committed_event = FoulCommittedEvent.create(
                        result=None,
                        qualifiers=None,
                        **generic_event_kwargs,
                    )
                    new_events.append(foul_committed_event)
                    if "card" in foul_committed_kwargs:
                        card_kwargs = foul_committed_kwargs["card"]
                        card_event = CardEvent.create(
                            result=None,
                            qualifiers=None,
                            card_type=card_kwargs["card_type"],
                            **generic_event_kwargs,
                        )
                        new_events.append(card_event)
                elif event_type == SB_EVENT_TYPE_PLAYER_ON:
                    player_on_event = PlayerOnEvent.create(
                        result=None,
                        qualifiers=None,
                        **generic_event_kwargs,
                    )
                    new_events.append(player_on_event)
                elif event_type == SB_EVENT_TYPE_PLAYER_OFF:
                    player_off_event = PlayerOffEvent.create(
                        result=None,
                        qualifiers=None,
                        **generic_event_kwargs,
                    )
                    new_events.append(player_off_event)

                elif event_type == SB_EVENT_TYPE_RECOVERY:
                    recovery_event = RecoveryEvent.create(
                        result=None,
                        qualifiers=None,
                        **generic_event_kwargs,
                    )
                    new_events.append(recovery_event)

                # rest: generic
                else:
                    generic_event = GenericEvent.create(
                        result=None,
                        qualifiers=None,
                        event_name=raw_event["type"]["name"],
                        **generic_event_kwargs,
                    )
                    new_events.append(generic_event)

                for event in new_events:
                    if self.should_include_event(event):
                        transformed_event = transformer.transform_event(event)
                        events.append(transformed_event)

                    # Checks if the event ended out of the field and adds a synthetic out event
                    if event.result in OUT_EVENT_RESULTS:
                        generic_event_kwargs["ball_state"] = BallState.DEAD
                        if event.receiver_coordinates:
                            generic_event_kwargs[
                                "coordinates"
                            ] = event.receiver_coordinates

                            ball_out_event = BallOutEvent.create(
                                result=None,
                                qualifiers=None,
                                **generic_event_kwargs,
                            )

                            if self.should_include_event(ball_out_event):
                                transformed_ball_out_event = (
                                    transformer.transform_event(ball_out_event)
                                )
                                events.append(transformed_ball_out_event)

        metadata = Metadata(
            teams=teams,
            periods=periods,
            pitch_dimensions=transformer.get_to_coordinate_system().pitch_dimensions,
            frame_rate=None,
            orientation=Orientation.ACTION_EXECUTING_TEAM,
            flags=DatasetFlag.BALL_OWNING_TEAM,
            score=None,
            provider=Provider.STATSBOMB,
            coordinate_system=transformer.get_to_coordinate_system(),
        )

        return EventDataset(
            metadata=metadata,
            records=events,
        )