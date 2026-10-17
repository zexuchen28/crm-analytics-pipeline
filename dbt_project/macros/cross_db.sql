{#
  Cross-database helpers, so the same staging models run on DuckDB (local)
  and BigQuery. dbt picks <adapter>__<name> for the active adapter and falls
  back to default__<name>. Here default__ is the DuckDB implementation.

  The raw layer keeps each CRM record as a JSON string in `payload`;
  json_value() pulls one top-level field out of it as text.
#}

{% macro json_value(column, field) -%}
    {{ return(adapter.dispatch('json_value', 'crm_analytics')(column, field)) }}
{%- endmacro %}

{% macro default__json_value(column, field) -%}
    json_extract_string({{ column }}, '$.{{ field }}')
{%- endmacro %}

{% macro bigquery__json_value(column, field) -%}
    json_value({{ column }}, '$.{{ field }}')
{%- endmacro %}


{# CRM timestamps are ISO-8601 UTC strings such as 2026-02-01T21:16:13Z #}
{% macro to_ts(expr) -%}
    {{ return(adapter.dispatch('to_ts', 'crm_analytics')(expr)) }}
{%- endmacro %}

{% macro default__to_ts(expr) -%}
    strptime({{ expr }}, '%Y-%m-%dT%H:%M:%SZ')
{%- endmacro %}

{% macro bigquery__to_ts(expr) -%}
    timestamp({{ expr }})
{%- endmacro %}


{% macro to_date(expr) -%}
    {{ return(adapter.dispatch('to_date', 'crm_analytics')(expr)) }}
{%- endmacro %}

{% macro default__to_date(expr) -%}
    cast({{ expr }} as date)
{%- endmacro %}


{# Floating-point type name, spelled per database. #}
{% macro double_type() -%}
    {{ return(adapter.dispatch('double_type', 'crm_analytics')()) }}
{%- endmacro %}

{% macro default__double_type() -%}double{%- endmacro %}

{% macro bigquery__double_type() -%}float64{%- endmacro %}


{# Division that returns NULL instead of failing when the denominator is 0 or NULL. #}
{% macro safe_divide(numerator, denominator) -%}
    (cast({{ numerator }} as {{ double_type() }}) / nullif(cast({{ denominator }} as {{ double_type() }}), 0))
{%- endmacro %}


{# Elapsed days (fractional) between two timestamps; NULL if either is NULL. #}
{% macro days_between(start_ts, end_ts) -%}
    {{ return(adapter.dispatch('days_between', 'crm_analytics')(start_ts, end_ts)) }}
{%- endmacro %}

{% macro default__days_between(start_ts, end_ts) -%}
    (cast(date_diff('second', {{ start_ts }}, {{ end_ts }}) as double) / 86400.0)
{%- endmacro %}

{% macro bigquery__days_between(start_ts, end_ts) -%}
    (timestamp_diff({{ end_ts }}, {{ start_ts }}, second) / 86400.0)
{%- endmacro %}


{#
  One row per element of a JSON array stored as text.
    relation    name of the table/CTE holding the JSON
    id_column   column to carry through
    json_column column holding the JSON text
    path        JSONPath to the array, e.g. '$.stage_history'
  Returns a SELECT with columns (id_column, element).
#}
{% macro json_array_elements(relation, id_column, json_column, path) -%}
    {{ return(adapter.dispatch('json_array_elements', 'crm_analytics')(relation, id_column, json_column, path)) }}
{%- endmacro %}

{% macro default__json_array_elements(relation, id_column, json_column, path) -%}
    select {{ id_column }}, unnest(from_json(json_extract({{ json_column }}, '{{ path }}'), '["JSON"]')) as element
    from {{ relation }}
{%- endmacro %}

{% macro bigquery__json_array_elements(relation, id_column, json_column, path) -%}
    select t.{{ id_column }}, element
    from {{ relation }} as t, unnest(json_query_array(t.{{ json_column }}, '{{ path }}')) as element
{%- endmacro %}
