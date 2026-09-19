"""Shared transport-independent events. Acquisition uses monotonic timestamps."""
from dataclasses import dataclass, field
import math
import time
import uuid


@dataclass(frozen=True)
class Shot:
    source: str  # "mevo" or "webcam"
    speed_mph: float
    hla: float
    vla: float = 0.0
    spin_rpm: float = 0.0
    spin_axis: float = 0.0
    club_speed_mph: float | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    captured_at: float = field(default_factory=time.monotonic)
    raw: dict = field(default_factory=dict)

    def validate(self) -> None:
        values = (self.speed_mph, self.hla, self.vla, self.spin_rpm, self.spin_axis)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            raise ValueError("Shot readings must be finite numbers")
        if self.source not in {"mevo", "webcam"}:
            raise ValueError("Unknown shot source")
        if not 0 < self.speed_mph <= 250:
            raise ValueError("Ball speed is outside the supported range")
        if not -90 <= self.hla <= 90 or not -30 <= self.vla <= 90:
            raise ValueError("Launch angle is outside the supported range")
        if not 0 <= self.spin_rpm <= 20000 or not -180 <= self.spin_axis <= 180:
            raise ValueError("Spin reading is outside the supported range")
        if self.source == "webcam" and self.speed_mph > 35:
            raise ValueError("Putt speed is outside the supported range")

    def gspro_payload(self, shot_number: int) -> dict:
        self.validate()
        data = {
            "DeviceID": "MevoCompanion",
            "Units": "Yards",
            "ShotNumber": shot_number,
            "APIversion": "1",
            "BallData": {"Speed": self.speed_mph, "HLA": self.hla,
                         "VLA": self.vla, "TotalSpin": self.spin_rpm,
                         "SpinAxis": self.spin_axis},
            "ShotDataOptions": {"ContainsBallData": True,
                                "ContainsClubData": self.club_speed_mph is not None,
                                "LaunchMonitorIsReady": True,
                                "LaunchMonitorBallDetected": True,
                                "IsHeartBeat": False},
        }
        if self.club_speed_mph is not None:
            data["ClubData"] = {"Speed": self.club_speed_mph}
        return data
