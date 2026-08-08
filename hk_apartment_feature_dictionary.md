# Hong Kong Apartment Feature Dictionary (Forecasting-Oriented)

This dictionary lists apartment-relevant columns for future price prediction, with source websites and feasibility.

## Legend

- Availability: `Ready` (directly available), `Derived` (can be engineered), `Partial` (available for some records), `Hard` (not reliably available at scale)
- Difficulty: `Easy`, `Medium`, `Hard`

## Core Transaction Features

| Column | Description | Source | Join Key | Availability | Difficulty |
|---|---|---|---|---|---|
| `sale_price` | Transaction amount (HKD) | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `asp_date` | Agreement for sale and purchase date | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `sale_year` | Year extracted from transaction date | Derived from `asp_date` | row-level | Derived | Easy |
| `sale_month` | Month extracted from transaction date | Derived from `asp_date` | row-level | Derived | Easy |
| `saleable_area` | Saleable area of flat | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `price_per_sq_m` | Unit price per square meter | `sale_price / saleable_area` | row-level | Derived | Easy |
| `scheme_type` | HOS / GSH etc | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `scheme_name` | Sale scheme label | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `court_estate_name` | Estate / court name | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `block_name` | Block / tower name | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `floor_num` | Floor string | Housing Authority SSFS export APIs | row-level | Ready | Easy |
| `floor_num_num` | Numeric floor | Parsed from `floor_num` | row-level | Derived | Easy |
| `flat_num` | Unit number string | Housing Authority SSFS export APIs | row-level | Ready | Easy |

## Estate / Building Features

| Column | Description | Source | Join Key | Availability | Difficulty |
|---|---|---|---|---|---|
| `district_name` | District | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `region_name` | HK Island / Kowloon / NT etc | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `year_of_completion` | Completion year | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `building_age_at_sale` | Sale year - completion year | Derived | row-level | Derived | Easy |
| `type_of_block` | Block type | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `no_of_blocks` | Number of blocks in estate | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `no_of_flats` | Number of flats in estate | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `map_latitude` | Estate latitude | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `map_longitude` | Estate longitude | Housing Authority `hos-courts` API | normalized estate name | Ready | Easy |
| `court_initial_sale_price_low` | Initial sale price lower bound | Housing Authority `hos-courts` API | normalized estate name | Partial | Medium |
| `court_initial_sale_price_high` | Initial sale price upper bound | Housing Authority `hos-courts` API | normalized estate name | Partial | Medium |

## Market History / Macro Features

| Column | Description | Source | Join Key | Availability | Difficulty |
|---|---|---|---|---|---|
| `rvd_price_classA_hk` | Monthly class A price index/value (HK Island) | RVD monthly price CSV `1.2M.csv` | sale month | Ready | Medium |
| `rvd_rent_classA_hk` | Monthly class A rent index/value (HK Island) | RVD monthly rent CSV `1.1M.csv` | sale month | Ready | Medium |
| `market_price_mom` | Month-over-month market move | Derived from RVD series | sale month | Derived | Medium |
| `market_price_yoy` | Year-over-year market move | Derived from RVD series | sale month | Derived | Medium |
| `market_rent_mom` | Month-over-month rent move | Derived from RVD series | sale month | Derived | Medium |
| `market_rent_yoy` | Year-over-year rent move | Derived from RVD series | sale month | Derived | Medium |
| `district_median_income` | District income context | C&SD district table 130-06806 | district + year | Partial | Medium |
| `district_household_size` | District household size | C&SD district table 130-06806 | district + year | Partial | Medium |

## Forecast Targets (for Future Price Prediction)

| Column | Description | Construction |
|---|---|---|
| `future_price_3m` | Median estate unit price 3 months ahead | Shifted by estate-level monthly panel |
| `future_price_6m` | Median estate unit price 6 months ahead | Shifted by estate-level monthly panel |
| `future_price_12m` | Median estate unit price 12 months ahead | Shifted by estate-level monthly panel |
| `future_return_3m` | `(future_price_3m / current_price) - 1` | Derived |
| `future_return_6m` | `(future_price_6m / current_price) - 1` | Derived |
| `future_return_12m` | `(future_price_12m / current_price) - 1` | Derived |

## Apartment-Specific but Currently Hard Fields

These are useful, but not reliably available as clean public bulk data for each transaction row:

- `unit_direction` / `facing`
- `layout_bedrooms`, `layout_bathrooms`
- `balcony`, `utility_platform`, `maid_room`
- `management_fee_psf`
- `renovation_condition`
- `view_type` (sea/park/open)
- `car_park_included`

Potential source candidates are Centaline / Midland / 28Hse listing pages, but extraction quality and consistency are mixed and may require browser automation and normalization.