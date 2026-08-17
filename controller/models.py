"""Validated HTTP models for the Water Controller Node API."""

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, field_validator, model_validator


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    hardware_id: str = Field(pattern=r"^[0-9A-F]{12}$")
    hostname: str = Field(min_length=1, max_length=253)
    ip: IPvAnyAddress
    type: str = Field(min_length=1, max_length=64)
    firmware: str = Field(min_length=1, max_length=32)

    @field_validator("hostname")
    @classmethod
    def hostname_must_be_local(cls, value: str) -> str:
        if not value.endswith(".local"):
            raise ValueError("hostname must end in .local")
        return value


class DeviceUpdateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=80)
    tank_height_cm: float | None = Field(default=None, gt=0, le=100_000)
    map_x: float | None = Field(default=None, ge=2, le=98)
    map_y: float | None = Field(default=None, ge=4, le=96)
    intersection_id: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def at_least_one_value(self) -> "DeviceUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("At least one setting is required")
        return self


class LinkCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source_device_id: str = Field(pattern=r"^water-[0-9]+$")
    target_device_id: str = Field(pattern=r"^water-[0-9]+$")
    label: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def different_nodes(self) -> "LinkCreateRequest":
        if self.source_device_id == self.target_device_id:
            raise ValueError("source and target must be different")
        return self


class AlertSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blockage_level1_cm: float = Field(gt=0, le=100_000)
    blockage_level2_cm: float = Field(gt=0, le=100_000)
    flood_level1_percent: float = Field(ge=0, le=100)
    flood_level2_percent: float = Field(ge=0, le=100)
    rain_level1_6h_mm: float = Field(ge=0, le=1_000)
    rain_level2_6h_mm: float = Field(gt=0, le=1_000)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> "AlertSettingsRequest":
        if self.blockage_level2_cm <= self.blockage_level1_cm:
            raise ValueError("Blockage L2 must be greater than L1")
        if self.flood_level2_percent <= self.flood_level1_percent:
            raise ValueError("Flood L2 must be greater than L1")
        if self.rain_level2_6h_mm <= self.rain_level1_6h_mm:
            raise ValueError("Rain L2 must be greater than L1")
        return self


class WeatherLocationCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=300)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = Field(default="Asia/Bangkok", min_length=1, max_length=80)
    admin1: str = Field(default="", max_length=120)
    admin2: str = Field(default="", max_length=120)
    admin3: str = Field(default="", max_length=120)
    country: str = Field(default="Việt Nam", max_length=120)


class IntersectionCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    weather_location_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=400)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
