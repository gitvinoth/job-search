# Databricks notebook source
# MAGIC %md
# MAGIC # ------------------------------------------------------------------------------------------
# MAGIC # Copyright 2025-26 Baker Hughes.  All rights reserved.
# MAGIC # This computer code is proprietary to Baker Hughes Company and/or its
# MAGIC # affiliate(s) and may be covered by patents. It may not be used, disclosed,
# MAGIC # modified, transferred, or reproduced without prior written consent.
# MAGIC # ------------------------------------------------------------------------------------------

# COMMAND ----------

from datetime import datetime
from delta.tables import DeltaTable
from pyspark.sql import Row
from pyspark.sql.functions import (
    col, lit, explode, when, lower, trim, array, expr, concat,
    row_number, desc, coalesce, current_timestamp, to_json, array_max,
    map_keys, first, flatten, array_distinct, max as spark_max,
    collect_list, monotonically_increasing_id, broadcast, map_values, size,
)
from pyspark.sql.window import Window
from pyspark.sql.types import (
    StructType, StructField, IntegerType, StringType,
    ArrayType, DoubleType, MapType,
)
from pyspark.sql.functions import max

# COMMAND ----------

try:
    if dbutils:
        pass  # pragma: no cover
except NameError:
    from src.utils.file_metadata_utility import add_epoch_timestamp
    from src.utils.read_utility import read_json, read_delta_table, read_table
    from src.utils.write_utility import write_table

# COMMAND ----------

PARAMETER_TABLE_MAPPING = {
    "pressure": "pt_gauge", "temperature": "pt_gauge",
    "surface_flow_rate": "flowmeter", "well_head_pressure": "flowmeter",
    "co2_injection_rate": "flowmeter", "vent_rate": "non_bh",
    "dts": "dss", "distributed_temperature": "dss",
    "axial_strain": "dss", "axial_strain_thermal": "dss",
    "bend_magnitude": "dss", "bend_mag": "dss", "curr_temp": "dss",
    "dts_temperature": "dts", "fiber_length": "dts",
    "data": "das",
    "magnitude": "microseismic_events",
    "no_of_events": "microseismic_events",
    "number_of_seismic_events": "microseismic_events",
    "value": "non_bh",
}

TABLE_TIMESTAMP_MAPPING = {
    "pt_gauge": "epoch_timestamp", "flowmeter": "timestamp",
    "dss": "timestamp", "dts": "timestamp",
    "das": "time", "microseismic_events": "epoch_timestamp", "non_bh": "timestamp",
}

DTS_PARAMETER_MAPPING  = {"dts_temperature": "temperature"}
DSS_PARAMETER_MAPPING  = {"axial_strain": "axial_strain_thermal", "bend_magnitude": "bend_mag", "dts": "curr_temp", "distributed_temperature": "curr_temp"}
FLOWMETER_PARAMETER_MAPPING = {"co2_injection_rate": "surface_flow_rate"}

TABLE_ZONE_MAPPING = {
    "pt_gauge": "silver_zone", "flowmeter": "silver_zone", "dss": "silver_zone",
    "dts": "silver_zone", "das": "silver_zone", "microseismic_events": "silver_zone", "non_bh": "silver_zone",
}

DEPTH_AWARE_TABLES       = ["dts", "dss", "das"]
ADD_FREQUENCY_DATA_TABLE = ["pt_gauge"]
DOWNSAMPLE_TABLES        = ["pt_gauge", "flowmeter", "non_bh"]
RULE_ID_MERGE_CONDITION  = "target.rule_id = source.rule_id"
DOWNSAMPLE_UNIT_MAP      = {60: "minute", 3600: "hour", 86400: "day"}
MULTI_PARAMETER_LABEL    = "All"

# COMMAND ----------

def get_table_name_for_parameter(parameter, sensor_type=None):
    """Map a parameter name to its silver-zone table, optionally using sensor_type as override."""
    if sensor_type and sensor_type.lower() in TABLE_ZONE_MAPPING:
        return sensor_type.lower()
    return PARAMETER_TABLE_MAPPING.get(parameter.lower(), "unknown_table")


def get_timestamp_column_for_table(table_name):
    """Return the timestamp column name for a given silver-zone table."""
    return TABLE_TIMESTAMP_MAPPING.get(table_name, "timestamp")


def get_actual_parameter_name(parameter, table_name):
    """Resolve the physical column name for a logical parameter on a given table."""
    p = parameter.lower()
    if table_name == "dts" and p in DTS_PARAMETER_MAPPING:
        return DTS_PARAMETER_MAPPING[p]
    if table_name == "dss" and p in DSS_PARAMETER_MAPPING:
        return DSS_PARAMETER_MAPPING[p]
    if table_name == "flowmeter" and p in FLOWMETER_PARAMETER_MAPPING:
        return FLOWMETER_PARAMETER_MAPPING[p]
    if table_name == "non_bh":
        return "value"
    return p


def get_zone_for_table(table_name):
    """Return the catalog zone string for a given table."""
    return TABLE_ZONE_MAPPING.get(table_name, "silver_zone")


def requires_depth_column(table_name):
    """Return True if the table has a depth dimension."""
    return table_name in DEPTH_AWARE_TABLES


def requires_data_frequency_column(table_name):
    """Return True if the asset query needs a data_frequency bound."""
    return table_name in ADD_FREQUENCY_DATA_TABLE


def supports_downsampling(table_name):
    """Return True if the table uses pt_gauge-style time-bucket downsampling."""
    return table_name in DOWNSAMPLE_TABLES


def filter_assets_for_parameter(asset_param_map, current_parameter):
    """Return asset IDs from the map that contain the given parameter."""
    if not asset_param_map or not current_parameter:
        return []
    p = current_parameter.lower()
    return [aid for aid, params in asset_param_map.items() if params and p in [x.lower() for x in params]]


def validate_location(asset_id, actual_location, constraint_map):
    """Return actual_location if it satisfies the constraint, else None."""
    if not constraint_map or asset_id not in constraint_map:
        return actual_location
    allowed = constraint_map.get(asset_id, [])
    if not allowed:
        return actual_location
    return actual_location if actual_location in allowed else None


def get_downsample_unit(downsample_time):
    """Return the SQL date_trunc unit string for a given downsample interval in seconds."""
    if not downsample_time:
        return None
    try:
        return DOWNSAMPLE_UNIT_MAP.get(int(downsample_time))
    except (ValueError, TypeError):
        return None

# COMMAND ----------

def _downsample_time_window(timestamp_col):
    """Return the pt_gauge-style bounded time window used for downsampled queries."""
    return timestamp_col + " BETWEEN ($end_time - $model_data_frequency) AND $end_time"


def build_downsampled_query(
    table_name, asset_id, value_column, zone, timestamp_col, downsample_time,
    extra_where=None, projection=None, output_alias=None,
):
    """Build a downsampled silver-zone query shared by pt_gauge, flowmeter, and non_bh."""
    full_table = "`$catalog`." + zone + "." + table_name
    where_parts = ["asset_id = '" + asset_id + "'", _downsample_time_window(timestamp_col)]
    if extra_where:
        where_parts.append(extra_where)
    where_clause = " WHERE " + " AND ".join(where_parts)
    unit = get_downsample_unit(downsample_time)
    select_value = projection if projection else value_column
    avg_alias = output_alias if output_alias else value_column
    if unit is None:
        return "SELECT " + timestamp_col + ", " + select_value + " FROM " + full_table + where_clause
    trunc_expr = "date_trunc('" + unit + "', to_timestamp(" + timestamp_col + "))"
    return (
        "SELECT asset_id, unix_timestamp(" + trunc_expr + ") AS " + timestamp_col
        + ", AVG(" + value_column + ") AS " + avg_alias
        + " FROM " + full_table + where_clause
        + " GROUP BY asset_id, " + trunc_expr
    )


def build_pt_gauge_downsampled_query(asset_id, actual_param, zone, timestamp_col, downsample_time):
    """Build a pt_gauge query with optional time-bucket downsampling."""
    return build_downsampled_query(
        "pt_gauge", asset_id, actual_param, zone, timestamp_col, downsample_time,
    )


def build_flowmeter_downsampled_query(asset_id, actual_param, parameter, zone, timestamp_col, downsample_time):
    """Build a flowmeter query with optional time-bucket downsampling."""
    projection = actual_param + " AS " + parameter if actual_param != parameter else actual_param
    output_alias = parameter if actual_param != parameter else actual_param
    return build_downsampled_query(
        "flowmeter", asset_id, actual_param, zone, timestamp_col, downsample_time,
        projection=projection, output_alias=output_alias,
    )


def build_non_bh_query(asset_id, parameter, zone, timestamp_col, downsample_time=None):
    """Build a non_bh query, optionally with pt_gauge-style downsampling."""
    tag_filter = "tag = '" + parameter + "'"
    if downsample_time and get_downsample_unit(downsample_time):
        return build_downsampled_query(
            "non_bh", asset_id, "value", zone, timestamp_col, downsample_time,
            extra_where=tag_filter,
        )
    return (
        "SELECT " + timestamp_col + ", value"
        + " FROM `$catalog`." + zone + ".non_bh"
        + " WHERE asset_id = '" + asset_id + "' AND " + tag_filter
        + " AND " + timestamp_col + " BETWEEN $start_time AND $end_time"
    )


def build_asset_query(asset_id, parameter, actual_param, sensor_type, wire_from, wire_to, downsample_time=None):
    """Build the silver-zone query for a single asset in an analytical rule."""
    asset_id     = asset_id     if asset_id     is not None else "UNKNOWN"
    parameter    = parameter    if parameter    is not None else "value"
    actual_param = actual_param if actual_param is not None else "value"
    sensor_type  = sensor_type  if sensor_type  is not None else "unknown"
    wire_from    = wire_from    if wire_from    is not None else 0
    wire_to      = wire_to      if wire_to      is not None else 1000
    zone          = get_zone_for_table(sensor_type)
    timestamp_col = get_timestamp_column_for_table(sensor_type)
    if sensor_type == "pt_gauge":
        return build_pt_gauge_downsampled_query(asset_id, actual_param, zone, timestamp_col, downsample_time)
    if sensor_type == "non_bh":
        return build_non_bh_query(asset_id, parameter, zone, timestamp_col, downsample_time)
    if sensor_type == "flowmeter" and downsample_time and get_downsample_unit(downsample_time):
        return build_flowmeter_downsampled_query(
            asset_id, actual_param, parameter, zone, timestamp_col, downsample_time,
        )
    if actual_param != parameter:
        projection = actual_param + " AS " + parameter
    else:
        projection = actual_param
    if requires_depth_column(sensor_type):
        select_clause = "SELECT " + timestamp_col + ", " + projection + ", depth"
    else:
        select_clause = "SELECT " + timestamp_col + ", " + projection
    from_clause = "FROM `$catalog`." + zone + "." + sensor_type
    if requires_data_frequency_column(sensor_type):
        where_clause = "WHERE asset_id = '" + asset_id + "' and " + timestamp_col + " BETWEEN ($start_time - $model_data_frequency) AND $end_time"
    else:
        where_clause = "WHERE asset_id = '" + asset_id + "' and " + timestamp_col + " BETWEEN $start_time AND $end_time"
    return select_clause + " " + from_clause + " " + where_clause

# COMMAND ----------

METADATA_VALUE_TYPE = StructType([
    StructField("asset_id",        StringType(), True),
    StructField("sensor_type",     StringType(), True),
    StructField("sensor_location", StringType(), True),
    StructField("parameter",       StringType(), True),
    StructField("query",           StringType(), True),
])

# COMMAND ----------

def is_dataframe_empty(df):
    """Return True if the DataFrame has no rows."""
    return len(df.limit(1).collect()) == 0

# COMMAND ----------

def get_analytical_rules_df(spark, logger, source_file_list):
    """Read analytical rule JSON files and return a DataFrame with trigger fields coalesced."""
    try:
        conditions_schema = StructType([
            StructField("condition_id",          IntegerType(),                                  True),
            StructField("condition_name",        StringType(),                                   True),
            StructField("asset_id",              ArrayType(StringType()),                        True),
            StructField("asset_parameters",      MapType(StringType(), ArrayType(StringType())), True),
            StructField("asset_locations",       MapType(StringType(), ArrayType(StringType())), True),
            StructField("join_condition",        StringType(),                                   True),
            StructField("operator",              StringType(),                                   True),
            StructField("class",                 IntegerType(),                                  True),
            StructField("threshold",             DoubleType(),                                   True),
            StructField("duration",              IntegerType(),                                  True),
            StructField("wire",                  StringType(),                                   True),
            StructField("wireLengthFrom",        IntegerType(),                                  True),
            StructField("wireLengthTo",          IntegerType(),                                  True),
            StructField("rule_run_frequency",    IntegerType(),                                  True),
            StructField("sensor_type",           StringType(),                                   True),
            StructField("function",              StringType(),                                   True),
            StructField("baseline_time",         IntegerType(),                                  True),
            StructField("threshold_unit",        StringType(),                                   True),
            StructField("additional_properties", MapType(StringType(), StringType()),            True),
        ])
        schema = StructType([
            StructField("rule_id",                IntegerType(),            True),
            StructField("selected_assetIds",      ArrayType(StringType()),  True),
            StructField("rule_name",              StringType(),             True),
            StructField("rule_type",              StringType(),             True),
            StructField("tenant_id",              StringType(),             True),
            StructField("mmrv_service_ids",       ArrayType(StringType()),  True),
            StructField("conditions",             ArrayType(conditions_schema), True),
            StructField("severity",               StringType(),             True),
            StructField("risk_register_controls", ArrayType(IntegerType()), True),
            StructField("file_name",              StringType(),             True),
            StructField("operation",              StringType(),             True),
            StructField("triggers_on_assetIds",   ArrayType(StringType()),  True),
            StructField("triggers_on_assetNames", ArrayType(StringType()),  True),
        ])
        EMPTY_ARR = array().cast(ArrayType(StringType()))
        rules_df = read_json(spark, logger, source_file_list, schema)
        rules_df = (
            rules_df
            .withColumn("well_id", coalesce(col("selected_assetIds"), array()))
            .withColumn("triggers_on_assetIds",   coalesce(col("triggers_on_assetIds"),   EMPTY_ARR))
            .withColumn("triggers_on_assetNames", coalesce(col("triggers_on_assetNames"), EMPTY_ARR))
        )
        return rules_df
    except Exception as e:
        logger.error("get_analytical_rules_df() | " + str(e))
        raise

# COMMAND ----------

def keep_latest_analytical_rules(logger, rules_df):
    """Deduplicate to the latest version of each rule_id."""
    try:
        rules_df = add_epoch_timestamp(logger, rules_df)
        window_spec = (
            Window.partitionBy("rule_id")
            .orderBy(desc("epoch_timestamp"))
            .rowsBetween(Window.unboundedPreceding, Window.currentRow)
        )
        rules_df = (
            rules_df.withColumn("row_num", row_number().over(window_spec))
            .filter(col("row_num") == 1)
            .drop("row_num", "epoch_timestamp")
        )
        return rules_df
    except Exception as e:
        logger.error("keep_latest_analytical_rules() | " + str(e))
        raise

# COMMAND ----------

def purge_rule_ids_from_tables(spark, logger, rule_ids_df, bronze_table_name, header_table_name, reason):
    """Delete matching rule_ids from both the bronze and header tables."""
    try:
        bronze_dt = read_delta_table(spark, logger, bronze_table_name)
        (bronze_dt.alias("target").merge(rule_ids_df.alias("source"), RULE_ID_MERGE_CONDITION).whenMatchedDelete().execute())
        header_dt = read_delta_table(spark, logger, header_table_name)
        (header_dt.alias("target").merge(rule_ids_df.alias("source"), RULE_ID_MERGE_CONDITION).whenMatchedDelete().execute())
        logger.info("purge_rule_ids_from_tables() | reason=" + reason)
    except Exception as e:
        logger.error("purge_rule_ids_from_tables() | reason=" + reason + " | " + str(e))
        raise

# COMMAND ----------

def _build_insert_df(spark, rules_df, create_ids, update_ids):
    """Build the DataFrame of rules to insert from create and update ID lists."""
    op_lower = lower(trim(col("operation")))
    if create_ids and update_ids:
        return rules_df.filter(op_lower.isin(["create", "update"]))
    if create_ids:
        return rules_df.filter(op_lower == "create")
    if update_ids:
        return rules_df.filter(op_lower == "update")
    return None

# COMMAND ----------

def separate_and_purge_operations(spark, logger, rules_df, bronze_table_name, header_table_name):
    """Classify rules by operation, purge existing records, and return the insert DataFrame."""
    try:
        op_rows    = rules_df.select("rule_id", lower(trim(col("operation"))).alias("op")).collect()
        delete_ids = list({r["rule_id"] for r in op_rows if r["op"] == "delete" and r["rule_id"] is not None})
        create_ids = list({r["rule_id"] for r in op_rows if r["op"] == "create" and r["rule_id"] is not None})
        update_ids = list({r["rule_id"] for r in op_rows if r["op"] == "update" and r["rule_id"] is not None})
        logger.info("separate_and_purge_operations() | delete=" + str(len(delete_ids)) + " | create=" + str(len(create_ids)) + " | update=" + str(len(update_ids)))
        id_schema = StructType([StructField("rule_id", IntegerType(), False)])
        if delete_ids:
            purge_rule_ids_from_tables(spark, logger, spark.createDataFrame([Row(rule_id=rid) for rid in delete_ids], schema=id_schema), bronze_table_name, header_table_name, "explicit-delete")
        if create_ids:
            purge_rule_ids_from_tables(spark, logger, spark.createDataFrame([Row(rule_id=rid) for rid in create_ids], schema=id_schema), bronze_table_name, header_table_name, "pre-purge-before-create")
        if update_ids:
            purge_rule_ids_from_tables(spark, logger, spark.createDataFrame([Row(rule_id=rid) for rid in update_ids], schema=id_schema), bronze_table_name, header_table_name, "pre-purge-before-update")
        return _build_insert_df(spark, rules_df, create_ids, update_ids)
    except Exception as e:
        logger.error("separate_and_purge_operations() | " + str(e))
        raise

# COMMAND ----------

def explode_analytical_conditions(logger, rules_df):
    """Explode the conditions array and populate individual columns for each condition."""
    try:
        exploded_df = rules_df.withColumn("condition_exploded", explode(col("conditions"))).select(
            "rule_id", "well_id", "rule_name", "tenant_id", "mmrv_service_ids",
            "rule_type", "operation", "file_name", "severity", "risk_register_controls",
            "triggers_on_assetIds",
            "triggers_on_assetNames",
            col("condition_exploded.condition_id").alias("condition_id"),
            col("condition_exploded.condition_name").alias("condition_name"),
            col("condition_exploded.asset_id").alias("asset_id"),
            col("condition_exploded.asset_parameters").alias("asset_parameter"),
            coalesce(col("condition_exploded.asset_locations"), lit(None).cast(MapType(StringType(), ArrayType(StringType())))).alias("asset_location_constraint"),
            coalesce(col("condition_exploded.join_condition"), lit("AND")).alias("join_condition"),
            coalesce(col("condition_exploded.operator"), lit(">")).alias("operator"),
            col("condition_exploded.class").alias("class"),
            coalesce(col("condition_exploded.threshold"), lit(0.0)).alias("threshold"),
            col("condition_exploded.duration").alias("duration"),
            col("condition_exploded.wire").alias("wire"),
            coalesce(col("condition_exploded.function"), lit("analytical")).alias("function"),
            col("condition_exploded.wireLengthFrom").alias("wire_length_from"),
            col("condition_exploded.wireLengthTo").alias("wire_length_to"),
            col("condition_exploded.rule_run_frequency").alias("rule_run_frequency"),
            coalesce(col("condition_exploded.sensor_type"), lit("to_be_derived")).alias("sensor_type"),
            coalesce(col("condition_exploded.baseline_time"), lit(0)).alias("baseline_time"),
            coalesce(col("condition_exploded.threshold_unit"), lit("std")).alias("threshold_unit"),
            col("condition_exploded.additional_properties").alias("additional_properties"),
            lit("POST").alias("method"),
            when(col("condition_exploded.function").isin(["moving_average", "exponential_smoothing"]), col("condition_exploded.duration")).otherwise(lit(0)).alias("window_slide_duration"),
        )
        return exploded_df
    except Exception as e:
        logger.error("explode_analytical_conditions() | " + str(e))
        raise

# COMMAND ----------

def _set_condition_parameter(df):
    """Keep one row per condition; set parameter to All when multiple parameters exist."""
    return (
        df
        .withColumn("all_parameters", array_distinct(flatten(map_values(col("asset_parameter")))))
        .withColumn(
            "parameter",
            when(size(col("all_parameters")) == 1, col("all_parameters").getItem(0))
            .otherwise(lit(MULTI_PARAMETER_LABEL)),
        )
        .drop("all_parameters")
    )


def _derive_sensor_type_analytical(final_df, asset_df):
    """Derive sensor_type per (rule_id, condition_id) from the asset table."""
    asset_lookup = broadcast(
        asset_df.select(col("asset_id").alias("lookup_asset_id"), col("asset_type").alias("derived_sensor_type")).distinct()
    )
    asset_param_exploded = (
        final_df.select("rule_id", "condition_id", "asset_parameter")
        .withColumn("map_asset_id", explode(map_keys(col("asset_parameter"))))
        .select("rule_id", "condition_id", "map_asset_id").distinct()
        .join(asset_lookup, col("map_asset_id") == col("lookup_asset_id"), "left")
        .groupBy("rule_id", "condition_id")
        .agg(first("derived_sensor_type", ignorenulls=True).alias("sensor_type"))
    )
    return final_df.drop("sensor_type").join(asset_param_exploded, on=["rule_id", "condition_id"], how="left")

# COMMAND ----------

def apply_join_condition_analytical(logger, exploded_df, asset_df):
    """Keep one bronze row per condition with combined asset_parameter metadata."""
    try:
        final_df = _set_condition_parameter(exploded_df)
        return _derive_sensor_type_analytical(final_df, asset_df)
    except Exception as e:
        logger.error("apply_join_condition_analytical() | " + str(e))
        raise

# COMMAND ----------

def _append_asset_metadata_entry(
    metadata_map, asset_id, param, asset_sensor_map, asset_location_map,
    asset_location_constraint, base_sensor, wire_from, wire_to, downsample_time,
):
    """Add one asset+parameter entry to the metadata map."""
    sensor_type = asset_sensor_map.get(asset_id, base_sensor)
    if not sensor_type or sensor_type == "to_be_derived":
        sensor_type = get_table_name_for_parameter(param)
    actual_param       = get_actual_parameter_name(param, sensor_type)
    actual_location    = asset_location_map.get(asset_id, "None")
    validated_location = validate_location(asset_id, actual_location, asset_location_constraint)
    sensor_location    = validated_location if validated_location else "None"
    query              = build_asset_query(asset_id, param, actual_param, sensor_type, wire_from, wire_to, downsample_time)
    composite_key      = asset_id + "__" + param
    metadata_map[composite_key] = {
        "asset_id": asset_id,
        "sensor_type": sensor_type,
        "sensor_location": sensor_location,
        "parameter": param,
        "query": query,
    }


def build_metadata_map_for_row(row, asset_sensor_map, asset_location_map, downsample_time):
    """Build the combined asset metadata map for a single condition row."""
    asset_parameter           = row["asset_parameter"]           or {}
    asset_location_constraint = row["asset_location_constraint"] or {}
    parameter                 = row["parameter"]
    base_sensor               = row["sensor_type"]
    wire_from                 = row["wire_length_from"]
    wire_to                   = row["wire_length_to"]
    metadata_map = {}

    if parameter and parameter != MULTI_PARAMETER_LABEL:
        relevant_assets = filter_assets_for_parameter(asset_parameter, parameter)
        for asset_id in relevant_assets:
            _append_asset_metadata_entry(
                metadata_map, asset_id, parameter, asset_sensor_map, asset_location_map,
                asset_location_constraint, base_sensor, wire_from, wire_to, downsample_time,
            )
    else:
        for asset_id, params in asset_parameter.items():
            if not params:
                continue
            for param in params:
                _append_asset_metadata_entry(
                    metadata_map, asset_id, param, asset_sensor_map, asset_location_map,
                    asset_location_constraint, base_sensor, wire_from, wire_to, downsample_time,
                )

    return metadata_map if metadata_map else None

# COMMAND ----------

def generate_asset_metadata(logger, rules_df, asset_df, downsample_time=None):
    """Build and join the asset_metadata map column for each analytical condition row."""
    try:
        all_rows = rules_df.select(
            "rule_id", "condition_id", "asset_parameter", "asset_location_constraint",
            "parameter", "sensor_type", "wire_length_from", "wire_length_to",
        ).collect()
        asset_sensor_map   = {row["asset_id"]: row["asset_type"]     for row in asset_df.select("asset_id", "asset_type").collect()}
        asset_location_map = {row["asset_id"]: row["asset_location"] for row in asset_df.select("asset_id", "asset_location").collect() if row["asset_location"] is not None}
        metadata_results = []
        malformed_keys = []
        for row in all_rows:
            if row["rule_id"] is None or row["condition_id"] is None:
                malformed_keys.append((row["rule_id"], row["condition_id"]))
                continue
            metadata_map = build_metadata_map_for_row(row, asset_sensor_map, asset_location_map, downsample_time)
            if metadata_map is None:
                continue
            metadata_results.append(Row(
                rule_id=row["rule_id"],
                condition_id=row["condition_id"],
                asset_metadata=metadata_map,
            ))
        if malformed_keys:
            logger.warning(
                "generate_asset_metadata() | Excluded "
                + str(len(malformed_keys))
                + " row(s) with null (rule_id, condition_id): "
                + str(malformed_keys)
            )
        clean_df = rules_df.filter(
            col("rule_id").isNotNull() & col("condition_id").isNotNull()
        )
        if not metadata_results:
            logger.info("generate_asset_metadata() | No matching assets — adding null metadata column")
            return clean_df.withColumn("asset_metadata", lit(None).cast(MapType(StringType(), METADATA_VALUE_TYPE)))
        metadata_schema = StructType([
            StructField("rule_id",        IntegerType(),                              False),
            StructField("condition_id",   IntegerType(),                              False),
            StructField("asset_metadata", MapType(StringType(), METADATA_VALUE_TYPE), True),
        ])
        metadata_df = clean_df.sparkSession.createDataFrame(metadata_results, schema=metadata_schema)
        return clean_df.join(metadata_df, on=["rule_id", "condition_id"], how="left")
    except Exception as e:
        logger.error("generate_asset_metadata() | " + str(e))
        raise

# COMMAND ----------

def add_rule_path(logger, rules_df):
    """Set the model path from the source file_name (e.g. 'anomaly_detection_das' -> '/anomaly_detection_das')."""
    try:
        return rules_df.withColumn(
            "path",
            when(col("file_name").startswith("/"), col("file_name"))
            .otherwise(concat(lit("/"), col("file_name"))),
        )
    except Exception as e:
        logger.error("add_rule_path() | " + str(e))
        raise

# COMMAND ----------

def add_data_frequency_analytical(logger, rules_df, asset_df):
    """Join asset data_frequency onto each analytical rule row."""
    try:
        asset_freq_df = broadcast(
            asset_df.select(col("asset_id"), col("data_frequency")).withColumn("data_frequency", 2 * col("data_frequency") - 1)
        )
        rules_df = rules_df.withColumn("unique_id", monotonically_increasing_id())
        freq_df = (
            rules_df.select("unique_id", explode("asset_id").alias("asset_id"))
            .join(asset_freq_df, on="asset_id", how="left")
            .groupBy("unique_id")
            .agg(
                collect_list("data_frequency").alias("data_frequency_list"),
                ((spark_max("data_frequency") + 1) / 2).cast(IntegerType()).alias("max_data_frequency"),
            )
        )
        result_df = rules_df.join(freq_df, on="unique_id", how="inner").withColumn(
            "window_slide_duration",
            when(col("function").isin(["moving_average", "exponential_smoothing"]), col("duration")).otherwise(lit(0)),
        )
        return result_df
    except Exception as e:
        logger.error("add_data_frequency_analytical() | " + str(e))
        raise

# COMMAND ----------

def insert_analytical_rules(logger, rules_df, bronze_table_name):
    """Select the final column set and write analytical rules to the bronze table."""
    try:
        EMPTY_ARR = array().cast(ArrayType(StringType()))
        bronze_df = rules_df.withColumn("last_updated_date", lit(datetime.now())).select(
            "rule_id", "well_id", "rule_name", "tenant_id", "mmrv_service_ids",
            "condition_id", "condition_name",
            array_distinct(flatten(array([map_keys(col("asset_parameter"))]))).alias("asset_id"),
            coalesce(col("triggers_on_assetIds"),   EMPTY_ARR).alias("parent_assetIds"),
            coalesce(col("triggers_on_assetNames"), EMPTY_ARR).alias("parent_assetNames"),
            col("asset_parameter"),
            col("asset_location_constraint").alias("asset_location"),
            "join_condition", "parameter", "operator", "class", "threshold", "duration",
            "wire", "function", "wire_length_from", "wire_length_to", "rule_run_frequency",
            "max_data_frequency", "sensor_type", "severity", "risk_register_controls",
            "baseline_time", "threshold_unit", "window_slide_duration",
            "asset_metadata", "method", "path", "last_updated_date",
        )
        write_table(logger, bronze_df, "append", bronze_table_name)
        logger.info("insert_analytical_rules() | Bronze ingestion complete")
    except Exception as e:
        logger.error("insert_analytical_rules() | " + str(e))
        raise

# COMMAND ----------

def create_analytical_rules_header(
    spark, logger, rules_json_df, bronze_table_name, bronze_analytical_rules_header_table,
):
    """Upsert the analytical rules header from the JSON source into the header table."""
    try:
        EMPTY_ARR = array().cast(ArrayType(StringType()))
        non_delete_rule_ids = (
            rules_json_df.filter(col("operation") != "delete").select("rule_id").dropDuplicates()
        )
        filtered_rules_df   = rules_json_df.join(non_delete_rule_ids, on="rule_id", how="inner")
        exploded_conditions = filtered_rules_df.withColumn("condition_exploded", explode(col("conditions")))
        rule_metadata = exploded_conditions.groupBy("rule_id").agg(
            first("well_id").alias("well_id"),
            first("rule_name").alias("rule_name"),
            first("tenant_id").alias("tenant_id"),
            first("mmrv_service_ids").alias("mmrv_service_ids"),
            first("severity").alias("severity"),
            first("risk_register_controls").alias("risk_register_controls"),
            lit("AND").alias("join_condition"),
        )
        rule_frequencies = filtered_rules_df.withColumn(
            "rule_run_frequency_list", expr("transform(conditions, x -> x.rule_run_frequency)")
        ).select("rule_id", array_max(col("rule_run_frequency_list")).alias("rule_run_frequency"))
        conditions_json = filtered_rules_df.select(
            "rule_id",
            to_json(col("conditions"), options={"ignoreNullFields": False}).alias("conditions"),
        )
        df_analytical = read_table(spark, logger, bronze_table_name)
        analytical_group_ids = df_analytical.groupBy("rule_id").agg(max("condition_id").alias("analytical_group_id"))
        parent_fields_df = filtered_rules_df.select(
            "rule_id", "triggers_on_assetIds", "triggers_on_assetNames"
        ).dropDuplicates(["rule_id"])
        result_header_df = (
            rule_metadata
            .join(rule_frequencies,     on="rule_id", how="inner")
            .join(conditions_json,      on="rule_id", how="inner")
            .join(analytical_group_ids, on="rule_id", how="inner")
            .join(parent_fields_df,     on="rule_id", how="left")
            .select(
                "rule_id", "well_id", "rule_name", "tenant_id", "mmrv_service_ids",
                "severity", "join_condition", "analytical_group_id",
                coalesce(col("triggers_on_assetIds"),   EMPTY_ARR).alias("parent_assetIds"),
                coalesce(col("triggers_on_assetNames"), EMPTY_ARR).alias("parent_assetNames"),
                "risk_register_controls", "conditions", "rule_run_frequency",
                current_timestamp().alias("last_updated_date"),
            )
        )
        target_dt = read_delta_table(spark, logger, bronze_analytical_rules_header_table)
        (
            target_dt.alias("target")
            .merge(result_header_df.alias("source"), RULE_ID_MERGE_CONDITION)
            .whenMatchedUpdateAll()
            .whenNotMatchedInsertAll()
            .execute()
        )
        logger.info("create_analytical_rules_header() | ingested successfully")
    except Exception as e:
        logger.error("create_analytical_rules_header() | " + str(e))
        raise

# COMMAND ----------

def log_analytical_rule_errors(spark, logger, error_rule_ids, source_file_list, bronze_error_log, job_id, run_id, task_id, workflow_name, task_name, error_message):
    """Write analytical rule processing errors to the error log table."""
    try:
        error_schema = StructType([
            StructField("workflow_job_id", StringType(), True), StructField("run_id", StringType(), True),
            StructField("task_id", StringType(), True), StructField("workflow_name", StringType(), True),
            StructField("task_name", StringType(), True), StructField("source", StringType(), True),
            StructField("error_message", StringType(), True), StructField("additional_context", StringType(), True),
            StructField("last_updated_date", StringType(), True),
        ])
        error_row = Row(
            workflow_job_id=str(job_id), run_id=str(run_id), task_id=str(task_id),
            workflow_name=str(workflow_name), task_name=str(task_name),
            source=str(source_file_list) if source_file_list else "unknown",
            error_message=error_message + " | rule_ids=" + (str(error_rule_ids) if error_rule_ids else "N/A"),
            additional_context="NA", last_updated_date=str(datetime.now()),
        )
        error_df = spark.createDataFrame([error_row], schema=error_schema)
        write_table(logger, error_df, "append", bronze_error_log)
        logger.info("log_analytical_rule_errors() | Logged to " + bronze_error_log)
    except Exception as e:
        logger.error("log_analytical_rule_errors() | Failed: " + str(e))

# COMMAND ----------

def de_load_analytical_rules_bronze(
    spark, logger, source_file_list, bronze_table_name, header_table_name,
    asset_table_name, downsample_time=None, bronze_error_log=None,
    job_id=None, run_id=None, task_id=None, workflow_name=None, task_name=None,
):
    """Orchestrate the full bronze ingestion pipeline for analytical rules."""
    try:
        logger.info("de_load_analytical_rules() | Stage 1 — Reading source files")
        rules_df = get_analytical_rules_df(spark, logger, source_file_list)
        if is_dataframe_empty(rules_df):
            logger.info("de_load_analytical_rules() | No rules found in source files")
            return
        logger.info("de_load_analytical_rules() | Stage 2 — Deduplicating to latest per rule_id")
        rules_df = keep_latest_analytical_rules(logger, rules_df)
        logger.info("de_load_analytical_rules() | Stage 3 — Separating operations and pre-purging")
        asset_df  = read_delta_table(spark, logger, asset_table_name).toDF()
        insert_df = separate_and_purge_operations(spark, logger, rules_df, bronze_table_name, header_table_name)
        if insert_df is None or is_dataframe_empty(insert_df):
            logger.info("de_load_analytical_rules() | No create/update rules to process")
            return
        logger.info("de_load_analytical_rules() | Stage 4 — Exploding conditions")
        exploded_df = explode_analytical_conditions(logger, insert_df)
        logger.info("de_load_analytical_rules() | Stage 5 — Applying join condition logic")
        joined_df = apply_join_condition_analytical(logger, exploded_df, asset_df)
        logger.info("de_load_analytical_rules() | Stage 6 — Generating asset metadata")
        metadata_df = generate_asset_metadata(logger, joined_df, asset_df, downsample_time)
        logger.info("de_load_analytical_rules() | Stage 7 — Adding path and data frequency")
        metadata_df = add_rule_path(logger, metadata_df)
        metadata_df = add_data_frequency_analytical(logger, metadata_df, asset_df)
        valid_rules_df = metadata_df.filter(col("data_frequency_list").isNotNull())
        if is_dataframe_empty(valid_rules_df):
            logger.warning("de_load_analytical_rules() | No valid rules after data frequency validation")
            return
        logger.info("de_load_analytical_rules() | Stage 8 — Inserting into bronze table")
        insert_analytical_rules(logger, valid_rules_df, bronze_table_name)
        logger.info("de_load_analytical_rules() | Stage 9 — Upserting detailed header")
        create_analytical_rules_header(spark, logger, insert_df, bronze_table_name, header_table_name)
        logger.info("de_load_analytical_rules() | Completed successfully")
    except Exception as e:
        logger.error("de_load_analytical_rules() | Fatal error: " + str(e))
        if bronze_error_log:
            log_analytical_rule_errors(
                spark, logger, error_rule_ids=None, source_file_list=source_file_list,
                bronze_error_log=bronze_error_log, job_id=job_id, run_id=run_id, task_id=task_id,
                workflow_name=workflow_name, task_name=task_name,
                error_message="de_load_analytical_rules failed: " + str(e),
            )
        raise
