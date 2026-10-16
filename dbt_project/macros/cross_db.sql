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
    cast({{ expr }} as date)
{%- endmacro %}
