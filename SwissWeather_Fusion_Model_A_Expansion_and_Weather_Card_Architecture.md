# SwissWeather Fusion --- Model A Forecast Expansion & Stage 2 Weather Forecast Card Architecture

**Document type:** Architecture / Implementation Specification\
**Quality target:** ICS-quality (implementation-ready, testable,
traceable)\
**Baseline:** SwissWeather Fusion v0.1.28\
**Scope:** Stage 1 --- expand Model A forecast data and fusion; Stage 2
--- integrate an advanced Home Assistant weather forecast card\
**Status:** Proposed architecture for implementation\
**Date:** 2026-09-02

------------------------------------------------------------------------

## 1. Executive Summary

SwissWeather Fusion v0.1.28 already collects weather forecasts from
multiple independent providers/models, but Model A currently normalizes
and fuses only five numeric measurements:

-   temperature
-   humidity
-   mean-sea-level pressure
-   total precipitation
-   wind speed

This is now the principal limitation of the forecast presentation layer.

The upstream models already provide materially richer information. In
particular, the MeteoSwiss ICON CH1/CH2 and DWD ICON-D2 feeds exposed
through Open-Meteo provide explicit rain, snowfall, precipitation
probability, weather code, snow depth, wind direction/gusts, cloud cover
and several additional meteorological variables. SRF already parses
fresh snow, precipitation probability, dew point, apparent temperature,
wind direction, gusts, irradiance and weather symbols. Meteonomiqs
already returns precipitation probability and pressure, but is
deliberately outside Model A today. Meteoblue currently provides a
smaller normalized set plus predictability; its additional fields
require fixture/API verification before being promoted into the common
fusion contract.

The architecture in this document therefore separates the work into two
stages:

### Stage 1 --- Model A Forecast Expansion

Expand the provider acquisition and normalization layer, then expand
Model A's forecast representation so that information actually supplied
by the forecast models is retained instead of discarded.

The key principle is:

> **Do not infer a value when an upstream model provides the value
> directly.**

For example, `snowy` should no longer be inferred solely from
temperature and total precipitation when explicit snowfall/rain data is
available.

Stage 1 also introduces parameter-specific fusion strategies. Numeric
continuous parameters may be blended; categorical weather codes must not
be arithmetically averaged; precipitation components must preserve
physical meaning; and parameters without local ground truth must not
receive fabricated learned bias corrections.

### Stage 2 --- Advanced Weather Forecast Card

Use `ha-weather-forecast-card` as the primary presentation layer once
the expanded Model A forecast contract is available.

The card should expose the rich forecast through:

-   current conditions
-   daily forecast
-   hourly forecast
-   precipitation amount/probability
-   rain/snow indication
-   humidity
-   pressure
-   wind
-   gusts
-   dew point / apparent temperature where available
-   weather condition
-   selected advanced forecast metrics

The custom card must remain a presentation layer. It must not become a
second weather-calculation engine.

------------------------------------------------------------------------

# 2. Current Architecture Baseline

## 2.1 Current provider set

The v0.1.28 code defines the following five Model A forecast experts:

``` text
ch1
ch2
icon_d2
srf
meteoblue
```

This is defined by `ALL_FORECAST_SOURCES` in:

``` text
custom_components/swissweather_fusion/const.py
```

Meteonomiqs and CombiPrecip are deliberately outside Model A:

-   CombiPrecip is radar observation data and is a Model B input.
-   Meteonomiqs is currently used for Model B/nowcast-related
    information and its hourly forecast is persisted with a
    `meteonomiqs_` prefix specifically so it cannot accidentally enter
    Model A.

This architecture should not be broken simply to make the card richer.

## 2.2 Current Model A measurement set

`ModelABlendCoordinator` currently defines:

``` python
MEASUREMENTS = (
    "temperature",
    "humidity",
    "pressure",
    "precip",
    "wind_speed",
)
```

Reference:

``` text
custom_components/swissweather_fusion/coordinator.py
ModelABlendCoordinator.MEASUREMENTS
```

Every forecast hour is consequently reduced to:

``` text
datetime
native_temperature
humidity
native_pressure
native_precipitation
native_wind_speed
condition
```

The current `condition` is derived by `model_a.derive_condition()`.

## 2.3 Current Open-Meteo acquisition

`clients/open_meteo.py` requests only:

``` python
HOURLY_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "pressure_msl",
    "precipitation",
    "wind_speed_10m",
)
```

The response mapper likewise maps only:

``` text
temperature_2m       -> temperature
relative_humidity_2m -> humidity
pressure_msl         -> pressure
precipitation        -> precip
wind_speed_10m       -> wind_speed
```

Reference:

``` text
custom_components/swissweather_fusion/clients/open_meteo.py
lines 46-55
lines 134-150
```

This is the first hard data-loss boundary.

## 2.4 Current SRF acquisition

SRF is already richer than the common Model A contract.

`clients/srf.py` currently maps:

``` text
TTT_C              -> temperature
RELHUM_PERCENT     -> humidity
PRESSURE_HPA       -> pressure
RRR_MM             -> precip
DEWPOINT_C         -> srf_dewpoint
TTTFEEL_C          -> srf_feels_like
FRESHSNOW_MM       -> srf_freshsnow
SUN_MIN            -> srf_sun_minutes
IRRADIANCE_WM2     -> srf_irradiance
PROBPCP_PERCENT    -> srf_precip_probability
DD_DEG             -> srf_wind_direction
symbol_code        -> srf_symbol_code
symbol24_code      -> srf_symbol24_code
```

Wind fields additionally include:

``` text
FF_KMH -> wind_speed
FX_KMH -> srf_wind_gust
```

Reference:

``` text
custom_components/swissweather_fusion/clients/srf.py
lines 391-411
```

Therefore SRF is already a concrete example of provider data being
collected but not entering the common fusion model.

## 2.5 Current Meteonomiqs acquisition

Meteonomiqs currently persists three hourly forecast fields:

``` text
meteonomiqs_pressure
meteonomiqs_precip_sum
meteonomiqs_precip_probability
```

The values are intentionally prefixed and excluded from
`ALL_FORECAST_SOURCES`.

Reference:

``` text
custom_components/swissweather_fusion/coordinator.py
Meteonomiqs forecast persistence path
```

Meteonomiqs must not be silently added to Model A merely because it has
useful parameters. Its source role, API quota, schedule and calibration
strategy are materially different.

## 2.6 Current Meteoblue acquisition

The current Meteoblue common mapping is:

``` text
temperature
relativehumidity
sealevelpressure
precipitation
windspeed
```

with `predictability` retained separately.

The current code also documents `rainspot` as a provider field whose
format was not sufficiently established for use in the normalized model.

Therefore Meteoblue must not receive invented snow/rain semantics. Its
additional parameters should enter the architecture only after
fixture/API verification.

------------------------------------------------------------------------

# 3. Architectural Objectives

## 3.1 Primary objectives

1.  Preserve weather parameters that the configured forecast models
    actually provide.
2.  Normalize equivalent parameters into common names and units.
3.  Keep provider-specific parameters when they are useful and verified.
4.  Fuse parameters only when a scientifically defensible fusion
    strategy exists.
5.  Never numerically average categorical weather codes.
6.  Never infer snow/rain from temperature when explicit
    precipitation-type data exists.
7.  Keep raw provider forecasts available for diagnostics and future
    model development.
8.  Keep Model A independent from Home Assistant UI implementation.
9.  Provide a stable forecast contract to the weather entity.
10. Make Stage 2 card configuration possible without modifying forecast
    calculations.
11. Preserve current Model A learning behavior for the three parameters
    that have local station ground truth.
12. Do not create artificial accuracy claims for parameters that cannot
    currently be reconciled against local observations.

## 3.2 Non-objectives

This change does not:

-   replace Model B;
-   turn Model A into machine learning;
-   add new weather providers;
-   make Meteonomiqs a Model A expert automatically;
-   invent local rain/snow ground truth;
-   attempt to calibrate categorical weather codes numerically;
-   make the custom Lovelace card responsible for meteorological
    calculations;
-   discard the existing five-parameter Model A behavior without
    regression coverage.

------------------------------------------------------------------------

# 4. Data Source Audit --- Confirmed Baseline

The following matrix distinguishes what is **already collected**, what
is **confirmed available upstream**, and what must therefore be added to
the acquisition/normalization path.

  ----------------------------------------------------------------------------------------------
  Provider/model    Current Model A fields        Confirmed useful additional  Stage 1 treatment
                                                  fields                       
  ----------------- ----------------------------- ---------------------------- -----------------
  MeteoSwiss        temp, RH, pressure, precip,   rain, snowfall, snow depth,  Expand
  ICON-CH1          wind                          weather code, precipitation  acquisition and
                                                  probability, cloud cover,    normalize
                                                  wind direction/gust, dew     
                                                  point, apparent temperature, 
                                                  visibility, freezing level,  
                                                  snowfall height, sunshine    
                                                  duration, CAPE and other     
                                                  fields                       

  MeteoSwiss        temp, RH, pressure, precip,   same family as CH1           Expand
  ICON-CH2          wind                                                       acquisition and
                                                                               normalize

  DWD ICON-D2       temp, RH, pressure, precip,   rain, showers, snowfall,     Expand
                    wind                          snow depth, weather code,    acquisition and
                                                  cloud cover layers,          normalize
                                                  visibility, wind             
                                                  direction/gust, dew point,   
                                                  apparent temperature,        
                                                  freezing level, snowfall     
                                                  height, CAPE, sunshine       
                                                  duration and more            

  SRF               temp, RH, pressure, precip,   fresh snow, precip           Promote selected
                    wind                          probability, dew point,      fields into
                                                  apparent temp, wind          normalized schema
                                                  direction, gust, irradiance, 
                                                  sun minutes, weather symbols 

  Meteoblue         temp, RH, pressure, precip,   additional provider data     Keep current
                    wind; predictability          exists, but snow/rain        contract; verify
                                                  semantics are not            before promotion
                                                  sufficiently established by  
                                                  the current code contract    

  Meteonomiqs       separate Model B data; hourly richer                       Retain outside
                    pressure/precip/probability   precipitation/snow-related   Model A unless a
                    persisted                     API fields exist             deliberate
                                                                               source-role
                                                                               change is
                                                                               approved

  CombiPrecip       radar precipitation           radar accumulation / quality Remain Model B
                    observation                                                only
  ----------------------------------------------------------------------------------------------

Open-Meteo's current MeteoSwiss documentation explicitly describes rain
and snowfall as available derived variables and states that
precipitation type is based on native snow-line information.
Open-Meteo's DWD ICON documentation likewise exposes precipitation,
rain, showers, snowfall, snow depth, weather code, cloud cover,
visibility, wind direction/gusts, freezing level and snowfall height,
among others.

Sources:

-   https://open-meteo.com/en/docs/meteoswiss-api
-   https://open-meteo.com/en/docs/dwd-api
-   https://developers.home-assistant.io/docs/core/entity/weather/

------------------------------------------------------------------------

# 5. Target Model A Forecast Contract

## 5.1 Design principle

The target contract is a **wide normalized forecast record**.

Each hourly forecast point should be capable of containing:

``` text
datetime

temperature
templow              [daily aggregation only]
humidity

pressure
dew_point
apparent_temperature

precipitation
rain
showers
snowfall
precipitation_probability

snow_depth
snowfall_height
freezing_level_height

wind_speed
wind_bearing
wind_gust_speed

cloud_coverage
visibility

uv_index
sunshine_duration

weather_code
condition
is_daytime
```

Not every field must be populated by every source.

`None` means:

> The source/model does not provide a valid value for this parameter at
> this time.

It must never mean:

> We guessed zero.

## 5.2 Core common fields

The following become the primary Model A normalized forecast parameters:

### Atmospheric state

``` text
temperature
humidity
pressure
dew_point
apparent_temperature
```

### Precipitation

``` text
precipitation
rain
showers
snowfall
precipitation_probability
```

### Snow / freezing state

``` text
snow_depth
snowfall_height
freezing_level_height
```

### Wind

``` text
wind_speed
wind_bearing
wind_gust_speed
```

### Visibility / cloud

``` text
cloud_coverage
visibility
```

### Radiation / sun

``` text
uv_index
sunshine_duration
```

### Weather classification

``` text
weather_code
condition
is_daytime
```

These fields map naturally to Home Assistant's weather forecast contract
where Home Assistant supports them, including humidity, pressure,
precipitation, precipitation probability, wind bearing, wind gust speed,
dew point, apparent temperature, UV index and visibility.

Reference:

https://developers.home-assistant.io/docs/core/entity/weather/

------------------------------------------------------------------------

# 6. Parameter Classes and Fusion Rules

A major architectural requirement is that **not every parameter is fused
the same way**.

## 6.1 Class A --- Existing learned numeric parameters

These retain the existing Model A EMA/MOS mechanism:

``` text
temperature
humidity
pressure
```

These are currently reconcilable against local station measurements.

Existing architecture:

``` text
source forecast
    ↓
lead-time bucket
    ↓
EMA bias + error
    ↓
debiased source value
    ↓
weighted blend
```

No change to the mathematical behavior should be made merely because the
schema expands.

## 6.2 Class B --- Numeric forecast parameters without local ground truth

Initially:

``` text
precipitation
rain
showers
snowfall
snow_depth
snowfall_height
freezing_level_height
wind_speed
wind_bearing
wind_gust_speed
dew_point
apparent_temperature
cloud_coverage
visibility
uv_index
sunshine_duration
```

These may be fused, but **must not receive fake learned bias
corrections**.

Until appropriate observations exist, the default strategy is:

``` text
available source values
        ↓
unit-normalized values
        ↓
parameter-specific validity filtering
        ↓
availability-aware arithmetic blend
```

The result is a forecast consensus, not a learned correction.

If future local sensors become available, the parameter can be promoted
into the learning subsystem.

## 6.3 Class C --- Categorical parameters

Examples:

``` text
weather_code
condition
```

These are never averaged numerically.

Instead:

``` text
numeric precipitation components
        +
cloud / visibility / wind information
        +
provider categorical evidence
        ↓
deterministic condition resolver
```

The resolver must prefer explicit provider weather classification when
reliable and otherwise derive a condition from the normalized physical
parameters.

## 6.4 Class D --- Provider confidence metadata

Examples:

``` text
meteoblue predictability
source coverage count
number of contributing models
parameter availability mask
```

These are not weather values and must not be blended as though they
were.

They belong in:

``` text
forecast metadata / diagnostics
```

------------------------------------------------------------------------

# 7. Precipitation Architecture

This is the most important part of the expansion.

## 7.1 Preserve four separate concepts

The normalized model must distinguish:

``` text
precipitation
rain
showers
snowfall
```

where the provider semantics are known.

`precipitation` is the total precipitation amount.

`rain` is the rain component.

`snowfall` is snow accumulation expressed using the provider's
documented unit.

`showers` is the convective/showery component where supplied.

## 7.2 Do not reconstruct snowfall from temperature

The current `derive_condition()` contains a fallback rule:

``` text
precipitation > threshold
    and temperature <= 0°C
        -> snowy
```

That rule remains useful only as a **fallback**.

It must no longer be the primary snow classifier when explicit
snowfall/rain information is available.

## 7.3 Preferred precipitation-type decision

Priority:

1.  Explicit snowfall \> 0 and rain \> 0 → `snowy-rainy`
2.  Explicit snowfall \> 0 → `snowy`
3.  Explicit rain/showers \> threshold → `rainy` or `pouring`
4.  Provider weather code indicates snow → `snowy`
5.  Provider weather code indicates mixed precipitation → `snowy-rainy`
6.  Fallback temperature/precipitation heuristic
7.  Cloud/visibility heuristic
8.  clear / clear-night

This makes the existing condition function more physically defensible
without breaking its role as a fallback.

## 7.4 Precipitation probability

`precipitation_probability` is a percentage, not a precipitation amount.

It must never be summed or averaged together with millimetres.

The initial fusion strategy should be an availability-aware mean across
eligible forecast sources.

A future version may implement a reliability-weighted probability
ensemble once enough validation data exists.

------------------------------------------------------------------------

# 8. Snowfall Units and Semantic Safety

Snow is particularly vulnerable to unit mistakes.

Open-Meteo documents snowfall in centimetres and precipitation in
millimetres. The APIs may also expose snow water equivalent in
millimetres.

Therefore the normalized contract must explicitly distinguish:

``` text
snowfall_cm
```

from:

``` text
snow_water_equivalent_mm
```

If the common Model A schema chooses `snowfall` as the canonical field,
its unit must be explicitly fixed.

Recommended canonical unit:

``` text
snowfall: cm
```

because this matches the user-facing meteorological meaning of snowfall
depth/amount and the documented Open-Meteo snowfall representation.

If water-equivalent snowfall is needed for physical calculations, retain
it separately:

``` text
snow_water_equivalent
```

Never silently convert one into the other.

------------------------------------------------------------------------

# 9. Wind Expansion

The target wind contract becomes:

``` text
wind_speed
wind_bearing
wind_gust_speed
```

Open-Meteo's DWD and MeteoSwiss APIs provide wind direction and gust
information.

SRF already provides:

``` text
DD_DEG
FF_KMH
FX_KMH
```

with the latter two converted to m/s in the current client.

The common unit must remain:

``` text
wind_speed       m/s
wind_gust_speed  m/s
wind_bearing     degrees
```

The existing `wind_speed` behavior must remain regression-tested.

Wind direction must not be averaged naïvely across 0°/360°.

If it is fused numerically, use circular/vector averaging.

Example:

``` text
350° + 10°
```

must produce approximately:

``` text
0°
```

not:

``` text
180°
```

------------------------------------------------------------------------

# 10. Pressure, Dew Point and Apparent Temperature

Pressure remains mean sea-level pressure.

This preserves the v0.1.24 fix that corrected the previous
surface-vs-MSL mismatch.

Dew point and apparent temperature are derived/forecast quantities and
should be treated as first-class forecast parameters when supplied.

If a provider supplies a native/official value, prefer that value over
locally recomputing it.

If no provider supplies it but sufficient normalized inputs exist, a
derived value may be generated only if the derivation is:

-   documented;
-   unit-tested;
-   clearly marked as derived;
-   deterministic.

The architecture must not produce two competing values for the same
parameter without recording provenance.

------------------------------------------------------------------------

# 11. Cloud, Visibility and Weather Condition

The current condition logic uses high humidity as a proxy for cloudiness
because the common model currently lacks cloud cover.

Once cloud coverage is available, the priority becomes:

``` text
explicit weather code
        ↓
cloud coverage + precipitation + visibility
        ↓
fallback humidity heuristic
```

The current humidity-based cloudy rule remains as a fallback only.

The following Home Assistant conditions should be used where supported:

``` text
sunny
clear-night
partlycloudy
cloudy
fog
rainy
pouring
snowy
snowy-rainy
hail
lightning
lightning-rainy
windy
windy-variant
```

Reference:

https://developers.home-assistant.io/docs/core/entity/weather/

------------------------------------------------------------------------

# 12. Weather Code Handling

Weather codes are source/model classification information.

They must be stored as:

``` text
weather_code
```

and associated with the provider where needed:

``` text
ch1_weather_code
ch2_weather_code
icon_d2_weather_code
...
```

A numeric average such as:

``` text
code 3 + code 71 / 2
```

has no meteorological meaning.

The fused `condition` should instead be resolved through a deterministic
precedence/consensus algorithm.

## 12.1 Recommended condition resolver

Input:

``` text
rain
snowfall
showers
precipitation
precipitation_probability
cloud_coverage
visibility
wind_speed
wind_gust_speed
weather_code(s)
temperature
is_daytime
```

Output:

``` text
Home Assistant condition
```

The resolver should produce both:

``` text
condition
condition_confidence
```

internally, if practical.

`condition_confidence` does not need to become a Home Assistant weather
property; it is useful for diagnostics and testing.

------------------------------------------------------------------------

# 13. Source Availability and Missing Data

A source may have:

-   shorter forecast horizon;
-   missing parameter;
-   null value;
-   temporarily incomplete response.

The fusion algorithm must be parameter-specific.

Example:

``` text
CH1:
temperature ✓
snowfall ✓
gust ✓

CH2:
temperature ✓
snowfall ✓
gust ✗

D2:
temperature ✓
snowfall ✓
gust ✓
```

For snowfall:

``` text
3 contributors
```

For gust:

``` text
2 contributors
```

The absence of gust from CH2 must not make the whole forecast hour
unavailable.

## 13.1 Minimum contribution policy

Each parameter should define its own minimum valid contributor count.

Recommended default:

``` text
1 valid source -> publish source value
2+ valid sources -> fuse
0 valid sources -> None
```

For high-risk derived classifications, one source may be insufficient to
override a stronger physical signal unless that source provides an
explicit provider classification.

------------------------------------------------------------------------

# 14. Model A Storage Changes

## 14.1 Forecast snapshots

The existing `forecast_snapshots` table is already a narrow structure:

``` text
source
issued_at
valid_at
variable
value
status
```

This is advantageous.

No wide SQL schema is required for every new weather parameter.

New variables can be stored as additional `variable` values.

Examples:

``` text
temperature
humidity
pressure
precip

rain
showers
snowfall
precip_probability

snow_depth
snowfall_height
freezing_level_height

wind_speed
wind_bearing
wind_gust_speed

dew_point
apparent_temperature

cloud_coverage
visibility

uv_index
sunshine_duration
weather_code
```

Provider-specific values may be stored with a namespace:

``` text
srf_dewpoint
srf_freshsnow
srf_symbol_code
meteoblue_predictability
```

## 14.2 Naming rule

Common normalized variables:

``` text
<canonical_name>
```

Provider-specific variables:

``` text
<provider>_<provider_name>
```

No provider-specific variable may use a canonical name unless its units
and semantics have been normalized.

------------------------------------------------------------------------

# 15. Fingerprinting and Deduplication

The existing run fingerprint logic must be expanded.

Current Open-Meteo fingerprinting intentionally hashes only the
variables represented by `_VARIABLE_NAME_MAP`.

Once more variables are acquired, the fingerprint must include the
expanded normalized acquisition set.

Otherwise:

``` text
rain/snow changes
```

could occur without changing the fingerprint, causing a new forecast run
to be incorrectly treated as unchanged.

Therefore the fingerprint must cover all provider values that affect
persisted forecast output.

Test requirement:

``` text
changing only snowfall
```

must change the run fingerprint.

Likewise:

``` text
changing only precipitation_probability
```

must change the fingerprint.

------------------------------------------------------------------------

# 16. Model A Learning Expansion

The existing learning coordinator reconciles:

``` text
temperature
humidity
pressure
```

against station measurements.

That remains correct.

## 16.1 Do not falsely "learn" precipitation

The current installation does not provide local rain/wind ground truth.

Therefore:

``` text
precipitation
rain
snowfall
wind_speed
wind_gust_speed
```

must not automatically enter EMA learning.

Their storage and fusion should be independent of the learning loop.

## 16.2 Future sensor promotion

If the integration later gains:

``` text
rain rate / rain accumulation
anemometer
wind direction
snow depth
```

then those parameters can be promoted to the reconciliation set.

This must be an explicit architecture change with corresponding:

-   sensor configuration;
-   unit normalization;
-   observation storage;
-   reconciliation tolerance;
-   tests;
-   accuracy reporting.

------------------------------------------------------------------------

# 17. Parameter Metadata Registry

To prevent `if/else` proliferation, Stage 1 should introduce a parameter
registry.

Conceptually:

``` python
ForecastParameter(
    name="snowfall",
    unit="cm",
    kind="continuous",
    aggregation="sum",
    fusion="availability_weighted_mean",
    learnable=False,
    ha_forecast_field="snowfall",
)
```

Another:

``` python
ForecastParameter(
    name="wind_bearing",
    unit="deg",
    kind="circular",
    aggregation="vector_mean",
    fusion="circular_mean",
    learnable=False,
    ha_forecast_field="wind_bearing",
)
```

And:

``` python
ForecastParameter(
    name="weather_code",
    unit=None,
    kind="categorical",
    aggregation="mode_or_resolver",
    fusion="categorical",
    learnable=False,
    ha_forecast_field=None,
)
```

This registry becomes the authoritative definition of:

-   unit;
-   type;
-   fusion strategy;
-   aggregation strategy;
-   learning eligibility;
-   HA presentation mapping.

------------------------------------------------------------------------

# 18. Daily and Twice-Daily Aggregation

The current aggregation only summarizes:

``` text
temperature high/low
precipitation total
condition
```

The expanded aggregation must preserve meaningful parameter semantics.

## 18.1 Daily

Recommended:

``` text
temperature           -> max
templow               -> min

precipitation         -> sum
rain                  -> sum
showers               -> sum
snowfall              -> sum

precipitation_probability -> max

wind_speed            -> max or representative mean
wind_gust_speed       -> max
wind_bearing          -> dominant/vector-derived direction

snow_depth            -> representative/latest value
snowfall_height       -> representative value
freezing_level_height -> representative value

humidity              -> representative or max/min only if explicitly needed
pressure               -> representative/latest
cloud_coverage         -> representative/mean
visibility              -> minimum

uv_index               -> max
sunshine_duration      -> sum
```

## 18.2 Twice daily

For each 06:00--18:00 and 18:00--06:00 period:

``` text
temperature -> max daytime / min nighttime
precipitation -> sum
rain -> sum
snowfall -> sum
precipitation_probability -> max
wind_gust -> max
```

The existing timezone handling must be preserved.

The existing v0.1.28 clear-night correction must remain covered by
regression tests.

------------------------------------------------------------------------

# 19. Home Assistant Weather Entity Contract

The current weather entity supports:

``` text
FORECAST_HOURLY
FORECAST_DAILY
FORECAST_TWICE_DAILY
```

and exposes:

``` text
temperature
humidity
pressure
wind_speed
condition
```

Stage 1 should extend the entity where Home Assistant's `WeatherEntity`
contract supports the parameter:

``` text
native_dew_point
native_apparent_temperature
native_wind_gust_speed
wind_bearing
cloud_coverage
visibility
uv_index
```

For forecast points, expose the supported fields in the returned
`Forecast` dictionaries.

Home Assistant currently documents these forecast fields, including:

``` text
humidity
native_apparent_temperature
native_dew_point
native_precipitation
native_pressure
native_temperature
native_templow
native_wind_gust_speed
native_wind_speed
precipitation_probability
uv_index
wind_bearing
condition
cloud_coverage
visibility
```

Reference:

https://developers.home-assistant.io/docs/core/entity/weather/

## 19.1 Important limitation

Home Assistant's standard `Forecast` contract does not provide dedicated
standard fields for every provider-specific metric.

Therefore advanced values such as:

``` text
snowfall
rain component
showers
snow depth
snowfall height
freezing level
model confidence
```

must be handled deliberately.

Preferred options, in order:

1.  expose through the custom card if the card can consume arbitrary
    forecast fields;
2.  expose as dedicated sensor entities when they are current/aggregate
    values;
3.  extend the custom card to support explicitly declared custom
    forecast metrics.

Do not overload unrelated standard HA fields.

------------------------------------------------------------------------

# 20. Stage 2 --- Advanced Weather Forecast Card

## 20.1 Selected card

Use:

``` text
ha-weather-forecast-card
```

as the primary dashboard presentation layer.

The project is a Home Assistant custom card and supports chart-oriented
forecast presentation and attribute selection.

Current repository:

https://github.com/troinine/ha-weather-forecast-card

The card's current configuration model supports current attributes and
forecast chart/attribute selection. A current example from the project's
issue tracker shows use of:

``` yaml
forecast:
  mode: chart
  extra_attribute: wind_bearing

current:
  secondary_info_attribute: pressure
```

This confirms that the card is designed to consume the normal HA weather
forecast representation rather than requiring a separate weather-data
source.

------------------------------------------------------------------------

# 21. Stage 2 Presentation Design

The target dashboard should have four logical layers.

## 21.1 Layer 1 --- Current conditions

Display:

``` text
Condition icon + temperature

Humidity
Pressure
Wind
Wind gust
```

Optional:

``` text
Dew point
Feels-like
Visibility
UV
```

## 21.2 Layer 2 --- Daily forecast

Display:

``` text
day
condition
high / low
precipitation
precipitation probability
snow indication
```

Where the card cannot natively display snowfall amount, use a dedicated
secondary metric or custom card enhancement rather than hiding the
information.

## 21.3 Layer 3 --- Hourly chart

Primary chart modes:

``` text
Temperature
Precipitation
```

Selectable overlays:

``` text
rain
snowfall
precipitation probability
humidity
pressure
wind speed
wind gust
```

The UI should avoid rendering every metric simultaneously.

The user needs a **metric selector**, not an overloaded graph.

## 21.4 Layer 4 --- Advanced details

A compact expandable area should expose:

``` text
snowfall
snow depth
snow line / snowfall height
freezing level
dew point
apparent temperature
visibility
cloud cover
UV
sunshine
```

Only metrics available for the relevant forecast period should be shown.

------------------------------------------------------------------------

# 22. Card Data Contract

Stage 2 should consume the output of Stage 1.

Conceptually:

``` text
weather.swissweather_fusion
        |
        +-- current state
        |
        +-- async_forecast_hourly()
        |
        +-- async_forecast_daily()
        |
        +-- async_forecast_twice_daily()
                    |
                    v
          ha-weather-forecast-card
```

The card must not:

-   call provider APIs;
-   read the SQLite database;
-   reproduce Model A fusion;
-   calculate provider weights;
-   decide whether CH1 is trustworthy;
-   infer snowfall from temperature.

Those responsibilities remain entirely in the integration.

------------------------------------------------------------------------

# 23. Card Enhancement Strategy

There are two implementation levels.

## Level A --- Configuration only

First determine whether the current card version can display the
required fields using:

``` text
standard HA Forecast fields
+
extra_attribute
+
current attribute configuration
```

If this is sufficient, do not fork or modify the card.

## Level B --- Small upstream-compatible card extension

If Level A cannot expose the expanded precipitation/snow metrics, extend
the card with a small generic concept:

``` yaml
forecast:
  metrics:
    - temperature
    - precipitation
    - rain
    - snowfall
    - precipitation_probability
    - humidity
    - pressure
    - wind_speed
    - wind_gust_speed
```

The card should render only metrics present in the forecast response.

This is preferable to hard-coding SwissWeather Fusion-specific names
into the card.

------------------------------------------------------------------------

# 24. Model Provenance

A major quality improvement is to retain provenance.

The fused forecast should be able to answer:

``` text
Which sources contributed to this value?
How many?
Which parameters were missing?
Was the value learned or unlearned?
```

For diagnostics, a forecast point may therefore carry metadata such as:

``` text
contributors:
  snowfall: [ch1, ch2, icon_d2, srf]
  wind_gust_speed: [ch1, icon_d2, srf]

coverage:
  snowfall: 4
  wind_gust_speed: 3

fusion_mode:
  snowfall: availability_weighted_mean
  wind_gust_speed: availability_weighted_mean
```

This metadata should not necessarily be exposed in the normal
user-facing card.

It is valuable for:

-   diagnostics;
-   debugging;
-   future accuracy work;
-   explaining unexpected forecasts.

------------------------------------------------------------------------

# 25. Source-Specific vs Fused Values

The architecture should distinguish:

### Fused weather entity

``` text
weather.swissweather_fusion
```

This is the user-facing forecast.

### Provider diagnostics

Existing source-specific values may remain available through diagnostic
sensors or internal storage.

Examples:

``` text
SRF fresh snow
Meteoblue predictability
CH1 snowfall
CH2 snowfall
D2 snowfall
```

This separation is important.

The weather card should show:

> **What SwissWeather Fusion believes.**

Diagnostics should show:

> **What each model said.**

------------------------------------------------------------------------

# 26. Accuracy and Confidence

The expanded Model A must not imply that all parameters are equally
accurate.

For every fused parameter we should track, internally:

``` text
number_of_sources
source coverage
learned/unlearned
fusion strategy
```

For the three learned parameters:

``` text
temperature
humidity
pressure
```

existing accuracy statistics remain authoritative.

For unlearned parameters:

``` text
precipitation
rain
snowfall
wind
...
```

do not expose a fabricated "accuracy %" until appropriate ground truth
exists.

------------------------------------------------------------------------

# 27. Testing Architecture

Stage 1 requires a substantially larger test matrix.

## 27.1 Provider parser tests

For each provider:

-   field present;
-   field absent;
-   null value;
-   malformed value;
-   array length mismatch;
-   unit conversion;
-   timestamp correctness;
-   fingerprint change.

## 27.2 Open-Meteo tests

Explicitly test:

``` text
rain
snowfall
snow_depth
precipitation_probability
weather_code
wind_direction
wind_gust
cloud_cover
dew_point
apparent_temperature
visibility
freezing_level_height
snowfall_height
```

for CH1, CH2 and D2.

Tests must verify that the URL actually requests the intended variables.

This is essential: a parser test alone is insufficient if the
acquisition URL never requests the field.

## 27.3 SRF tests

Extend the existing real-field tests for:

``` text
fresh snow
precip probability
dew point
feels-like
wind direction
gust
symbols
irradiance
```

## 27.4 Meteoblue tests

Do not add unverified fields merely to make the schema symmetrical.

First add real response fixtures.

Only then promote fields into the normalized contract.

## 27.5 Fusion tests

For every parameter:

``` text
1 source
2 sources
all sources
one missing source
all missing
zero-valued source
None-valued source
extreme value
```

## 27.6 Special fusion tests

### Snow

``` text
rain=0
snowfall>0
temperature>0
```

must still be `snowy` if explicit snowfall is supplied.

### Mixed precipitation

``` text
rain>0
snowfall>0
```

must become:

``` text
snowy-rainy
```

### Circular wind

``` text
350°, 10°
```

must not become 180°.

### Probability

``` text
80%, 20%
```

must remain a percentage calculation, never an amount.

------------------------------------------------------------------------

# 28. Regression Protection

All v0.1.28 regression tests must continue to pass.

In particular:

-   clear-night behavior;
-   timezone-correct daily aggregation;
-   twice-daily overnight low;
-   pressure normalization;
-   forecast fingerprinting;
-   provider run deduplication;
-   source health;
-   diagnostics redaction;
-   Model A learning;
-   existing five-parameter blend behavior.

The expansion must not silently change the meaning of the existing:

``` text
temperature
humidity
pressure
precip
wind_speed
```

fields.

------------------------------------------------------------------------

# 29. Performance Architecture

The current Model A coordinator already improved performance by fetching
forecast snapshots and bucket statistics in bulk and doing the blend in
memory.

The expanded model must preserve this pattern.

Do not introduce:

``` text
168 hours
× N parameters
× N sources
```

individual database queries.

The target remains:

``` text
bulk forecast query
+
bulk bucket query
+
in-memory fusion
```

The additional parameters increase CPU/memory use linearly, but the
forecast horizon remains bounded.

A seven-day hourly forecast with approximately 20 normalized parameters
is still tractable in memory.

------------------------------------------------------------------------

# 30. API Call and Quota Considerations

Expanding the Open-Meteo `hourly=` request does not conceptually require
additional calls.

The existing:

``` text
CH1
CH2
ICON-D2
```

requests can return more variables in the same forecast request.

The important controls are:

-   request only the variables actually needed;
-   avoid provider-specific variables that have no downstream consumer;
-   keep fingerprinting deterministic;
-   retain existing poll schedules.

For Meteonomiqs and Meteoblue, API quotas remain a hard architectural
constraint.

Stage 1 must not create additional calls merely because additional
fields exist.

------------------------------------------------------------------------

# 31. Recommended Implementation Sequence

## Stage 1A --- Canonical parameter registry

Create the parameter metadata registry.

Deliver:

-   canonical names;
-   units;
-   type;
-   fusion strategy;
-   aggregation strategy;
-   HA mapping;
-   learning eligibility.

## Stage 1B --- Open-Meteo expansion

Expand:

``` text
clients/open_meteo.py
```

to request and parse the agreed common parameter set.

Start with:

``` text
temperature
humidity
pressure

precipitation
rain
showers
snowfall
precipitation_probability

snow_depth
snowfall_height
freezing_level_height

wind_speed
wind_bearing
wind_gust_speed

dew_point
apparent_temperature

cloud_coverage
visibility

weather_code
```

Add additional radiation/UV fields only if confirmed useful and
supported consistently.

## Stage 1C --- SRF normalization

Promote verified SRF fields into the common schema:

``` text
dew_point
apparent_temperature
snowfall/fresh_snow
precipitation_probability
wind_bearing
wind_gust_speed
```

Keep SRF-specific symbols for provenance/diagnostics.

## Stage 1D --- Model A expansion

Expand:

``` text
ModelABlendCoordinator.MEASUREMENTS
```

but implement parameter-specific fusion strategies rather than forcing
every parameter through the existing EMA learning path.

## Stage 1E --- Condition resolver

Replace the current temperature-only snow inference with:

``` text
explicit precipitation type
        >
provider weather code
        >
physical fallback
```

## Stage 1F --- Aggregation

Extend daily and twice-daily aggregation for:

``` text
rain
snowfall
probability
wind/gust
```

and the other selected fields.

## Stage 1G --- WeatherEntity

Expose all supported HA weather fields.

## Stage 1H --- Tests

Add parser, normalization, fusion, aggregation and entity regression
tests.

Only after Stage 1 is green should Stage 2 begin.

------------------------------------------------------------------------

# 32. Stage 2 Implementation Sequence

## Stage 2A --- Install and baseline the card

Install `ha-weather-forecast-card` through HACS and verify the current
v0.1.28 weather entity.

## Stage 2B --- Standard fields first

Configure:

``` text
current:
  temperature
  humidity
  pressure
  wind

forecast:
  daily
  hourly
  precipitation
```

## Stage 2C --- Add advanced metrics

Expose:

``` text
snowfall
rain
precipitation probability
gust
dew point
apparent temperature
visibility
cloud cover
```

using the card's supported attribute mechanisms.

## Stage 2D --- Custom metric selector if required

If the current card cannot expose the full forecast contract, add a
generic metric selector rather than SwissWeather-specific code.

## Stage 2E --- UX validation

Test:

-   desktop;
-   mobile;
-   narrow dashboard columns;
-   hourly scrolling;
-   daily view;
-   night conditions;
-   snowfall;
-   mixed rain/snow;
-   missing metrics;
-   short model horizons.

------------------------------------------------------------------------

# 33. Acceptance Criteria --- Stage 1

Stage 1 is complete only when all of the following are true:

### Data acquisition

-   CH1 requests explicit rain/snow data.
-   CH2 requests explicit rain/snow data.
-   D2 requests explicit rain/snow data.
-   SRF verified fields are normalized.
-   No new provider calls are introduced solely for field expansion.

### Normalization

-   Canonical units are explicit.
-   `precipitation`, `rain`, `showers`, and `snowfall` remain distinct.
-   Snow water equivalent is not confused with snowfall depth.
-   Wind bearing uses degrees.
-   Wind speed/gust use m/s.

### Fusion

-   Existing learned temperature/humidity/pressure behavior remains
    intact.
-   New parameters do not receive fabricated EMA corrections.
-   Missing source parameters do not invalidate an otherwise usable
    forecast.
-   Categorical weather codes are never averaged.
-   Explicit snow information takes precedence over temperature
    inference.

### Weather entity

-   Hourly forecast contains the expanded supported fields.
-   Daily forecast contains meaningful aggregates.
-   Twice-daily forecast contains meaningful aggregates.
-   `condition` is physically consistent with precipitation type.

### Tests

-   All existing tests pass.
-   New parser fixtures cover each promoted field.
-   New fusion tests cover missing and mixed-source data.
-   New snow/rain condition tests exist.
-   Fingerprints change when newly retained fields change.

------------------------------------------------------------------------

# 34. Acceptance Criteria --- Stage 2

Stage 2 is complete only when:

-   `ha-weather-forecast-card` displays current conditions correctly.
-   Daily forecast works.
-   Hourly forecast works.
-   Precipitation amount is visible.
-   Precipitation probability is visible.
-   Rain/snow information is visible.
-   Humidity is visible.
-   Pressure is visible.
-   Wind and gust are visible where available.
-   Night conditions show `clear-night`.
-   Snow forecasts show `snowy` rather than `rainy`.
-   Missing advanced parameters do not break the card.
-   The card does not perform any provider/network/database access.
-   The integration remains fully functional if the custom card is
    removed.

------------------------------------------------------------------------

# 35. Risks and Mitigations

  -----------------------------------------------------------------------
  Risk                    Impact                  Mitigation
  ----------------------- ----------------------- -----------------------
  Provider semantics      High                    canonical registry +
  differ                                          explicit source
                                                  mappings

  Snow unit confusion     High                    separate snowfall and
                                                  snow-water-equivalent
                                                  fields

  Averaging categorical   High                    categorical resolver
  codes                                           

  No local precipitation  Medium                  no fabricated learning
  ground truth                                    

  Expanded fingerprints   Medium                  include all
  increase churn                                  output-affecting fields
                                                  deterministically

  Card does not support   Medium                  generic custom metric
  arbitrary forecast                              support or dedicated
  fields                                          sensors

  More forecast data      Medium                  existing 90-day
  increases DB size                               retention + parameter
                                                  review

  More UI metrics create  Medium                  metric selector and
  clutter                                         progressive disclosure

  Provider adds/removes   Medium                  parser fixtures +
  fields                                          diagnostics + graceful
                                                  `None` handling
  -----------------------------------------------------------------------

------------------------------------------------------------------------

# 36. Architectural Decision Records

## ADR-001 --- Expand Model A instead of adding card-specific data paths

**Decision:** The weather entity remains the single presentation data
contract.

**Reason:** The card must display the same forecast that Home Assistant
automations and other consumers receive.

**Rejected:** Card-specific provider calls.

------------------------------------------------------------------------

## ADR-002 --- Preserve provider information before fusion

**Decision:** Provider parsers retain verified parameters before Model A
reduces them.

**Reason:** Information cannot be recovered after fusion.

**Rejected:** Deriving snow/rain solely from the fused temperature and
total precipitation.

------------------------------------------------------------------------

## ADR-003 --- Do not use the existing EMA blindly for every new parameter

**Decision:** Only parameters with suitable ground truth participate in
learned calibration.

**Reason:** An EMA requires a meaningful forecast-vs-observation pair.

**Rejected:** Treating missing rain/wind observations as zero or using
unrelated station variables as ground truth.

------------------------------------------------------------------------

## ADR-004 --- Do not average weather codes

**Decision:** Weather codes remain categorical.

**Reason:** Their numeric values are labels, not continuous physical
measurements.

**Rejected:** arithmetic averaging.

------------------------------------------------------------------------

## ADR-005 --- Prefer generic card capabilities

**Decision:** If `ha-weather-forecast-card` needs enhancement, add
generic forecast-metric support rather than SwissWeather-specific
hard-coded logic.

**Reason:** The card should remain reusable and maintainable.

------------------------------------------------------------------------

# 37. Final Target Architecture

``` text
                   WEATHER PROVIDERS
                         |
       +-----------------+-----------------+
       |                 |                 |
   ICON CH1          ICON CH2          ICON D2
       |                 |                 |
       +-----------------+-----------------+
                         |
                       SRF
                         |
                    Meteoblue
                         |
                         v
              Provider-specific parsers
                         |
                         v
              Canonical parameter registry
                         |
                         v
               Normalized forecast points
                         |
              +----------+-----------+
              |                      |
       persisted snapshots       raw provenance
              |                      |
              +----------+-----------+
                         |
                         v
                 Model A Fusion
                         |
        +----------------+----------------+
        |                |                |
     learned          unlearned       categorical
     numeric           numeric          resolver
        |                |                |
        +----------------+----------------+
                         |
                         v
               Fused forecast contract
                         |
          +--------------+--------------+
          |              |              |
       current        hourly          daily /
                                      twice-daily
          |              |              |
          +--------------+--------------+
                         |
                         v
             Home Assistant WeatherEntity
                         |
                         v
              ha-weather-forecast-card
                         |
       +-----------------+------------------+
       |                 |                  |
    current           forecast           advanced
   conditions         charts             metrics
```

------------------------------------------------------------------------

# 38. Recommended End State

The resulting SwissWeather Fusion architecture should answer three
different questions cleanly:

### "What is the weather?"

The fused `weather.*` entity.

### "What do the individual models say?"

Provider-specific diagnostics and persisted snapshots.

### "Why does SwissWeather Fusion believe this?"

Model A provenance, contributor count, fusion method and
learned-vs-unlearned status.

That separation is the key architectural improvement.

The immediate implementation target is therefore **not simply a prettier
weather card**.

It is:

> **Make Model A retain and correctly fuse the meteorological
> information that the forecast models already provide, then make the
> advanced card expose that richer fused contract.**

This avoids throwing away valuable snow/rain/model information upstream
and avoids rebuilding weather logic inside the frontend.

------------------------------------------------------------------------

# 39. References

## SwissWeather Fusion v0.1.28 source

Key files:

``` text
custom_components/swissweather_fusion/clients/open_meteo.py
custom_components/swissweather_fusion/clients/srf.py
custom_components/swissweather_fusion/clients/meteoblue.py
custom_components/swissweather_fusion/clients/meteonomiqs.py
custom_components/swissweather_fusion/models/model_a.py
custom_components/swissweather_fusion/coordinator.py
custom_components/swissweather_fusion/weather.py
custom_components/swissweather_fusion/const.py
```

## Home Assistant weather entity

https://developers.home-assistant.io/docs/core/entity/weather/

## Home Assistant weather integration

https://www.home-assistant.io/integrations/weather/

## Home Assistant standard weather forecast card

https://www.home-assistant.io/dashboards/weather-forecast/

## Open-Meteo MeteoSwiss API

https://open-meteo.com/en/docs/meteoswiss-api

## Open-Meteo DWD ICON API

https://open-meteo.com/en/docs/dwd-api

## ha-weather-forecast-card

https://github.com/troinine/ha-weather-forecast-card

------------------------------------------------------------------------

# 40. Implementation Note

This document is an architecture and implementation specification. It
intentionally does **not** modify v0.1.28.

The next engineering step should be a controlled Stage 1 implementation
against this specification, followed by a full code audit and test run
before any Stage 2 Lovelace/card work is merged.
