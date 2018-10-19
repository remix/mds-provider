"""
Work with an MDS Provider database.
"""

import json
import mds
from mds.db import sql
from mds.json import read_data_file
import os
import pandas as pd
import psycopg2
import sqlalchemy


TEMP_SC = f"temp_{mds.STATUS_CHANGES}"
TEMP_TRIPS = f"temp_{mds.TRIPS}"


def data_engine(user, password, db, host, port):
    """
    Create a SQLAlchemy engine using the provided connection information.
    """
    url = f"postgresql://{user}:{password}@{host}:{port}/{db}"
    return sqlalchemy.create_engine(url)

def load_from_df(df, record_type, engine, table, before_load=None, stage_first=True):
    """
    Inserts data from a DataFrame matching the given MDS :record_type: schema to the :table:
    using the connection specified in :enging:.

    :before_load: is an optional transform to perform on the DataFrame before inserting its data.

    :stage_first: when True, implements a staged upsert via a temp table. The default is True.
    """
    # run any pre-processors to transform the df
    if before_load is not None:
        new_df = before_load(df)
        df = new_df if new_df is not None else df

    if not stage_first:
        # append the data to an existing table
        df.to_sql(table, engine, if_exists="append", index=False)
    else:
        # insert this DataFrame into a fresh temp table
        temp = f"TEMP_{table}"
        df.to_sql(temp, engine, if_exists="replace", index=False)

        # now insert from the temp table to the actual table
        with engine.begin() as conn:
            if record_type == mds.STATUS_CHANGES:
                query = sql.insert_status_changes_from(temp, table)
            elif record_type == mds.TRIPS:
                query = sql.insert_trips_from(temp, table)
            if query is not None:
                conn.execute(query)

def load_from_file(src, record_type, engine, table, before_load=None, stage_first=True):
    """
    Load the data file of type :record_type: at :src: in the table :table: using the connection
    defined by :engine:.

    :before_load: is an optional callback to pre-process a DataFrame before loading
    it into :table:.
    """
    # read the data file
    _, df = read_data_file(src, record_type)
    load_from_df(df, record_type, engine, table, before_load=before_load, stage_first=stage_first)

def load_from_records(records, record_type, engine, table, before_load=None, stage_first=True):
    """
    Load the array of :records: of type :record_type: into the table :table: using the connection defined by :engine:.

    :before_load: is an optional callback to pre-process a DataFrame before loading
    it into :table:.
    """
    if isinstance(records, list):
        if len(records) > 0:
            df = pd.DataFrame.from_records(records)
            load_from_df(df, record_type, engine, table, before_load=before_load, stage_first=stage_first)
        else:
            print("No records to load")

def load_from_source(source, record_type, engine, table, before_load=None, stage_first=True):
    """
    Load from a variety of file path or object sources into a :table: using the connection defined by conn.

    :source: could be:

      - a list of json file paths

      - a data page (the contents of a json file), e.g.
        {
            "version": "x.y.z",
            "data": {
                "record_type": [{
                    "trip_id": "1",
                    ...
                },
                {
                    "trip_id": "2",
                    ...
                }]
            }
        }

      - a list of data pages

      - a dict of { Provider : [data page] }
    """
    # source is a single data page
    if isinstance(source, dict) and "data" in source and record_type in source["data"]:
        records = source["data"][record_type]
        load_from_records(records, record_type, engine, table, before_load=before_load, stage_first=stage_first)

    # source is a list of data pages
    elif isinstance(source, list) and all([isinstance(s, dict) and "data" in s for s in source]):
        for page in source:
            load_from_source(page, record_type, engine, table, before_load=before_load, stage_first=stage_first)

    # source is a dict of Provider => list of data pages
    elif isinstance(source, dict) and all(isinstance(k, mds.providers.Provider) for k in source.keys()):
        for _, pages in source.items():
            load_from_source(pages, record_type, engine, table, before_load=before_load, stage_first=stage_first)

    # source is a list of file paths
    elif any([isinstance(s, str) and os.path.exists(s) for s in source]):
        for path in source:
            load_from_file(path, record_type, engine, table, before_load=before_load, stage_first=stage_first)

    # source is a single file path
    elif isinstance(source, str) and os.path.exists(source):
        load_from_file(source, record_type, engine, table, before_load=before_load, stage_first=stage_first)

    else:
        print(f"Couldn't recognize source type: {type(source)}. Skipping.")

def _json_cols_tostring(df, cols):
    """
    For each :cols: in the :df:, convert to a JSON string.
    """
    for col in [c for c in cols if c in df]:
        df[col] = df[col].apply(json.dumps)

def _add_missing_cols(df, cols):
    """
    For each :cols: not in the :df:, add it as an empty col.
    """
    new_cols = df.columns.tolist() + cols
    return df.reindex(columns=new_cols)

def load_status_changes(sources, engine, table=mds.STATUS_CHANGES, stage_first=True):
    """
    Load status_changes data from :sources: using the connection defined by :engine:.

    By default, stages the load into a temp table before upserting to the final destination.
    """
    def before_load(df):
        _json_cols_tostring(df, ["event_location"])
        return _add_missing_cols(df, ["battery_pct", "associated_trips"])

    load_from_source(
        sources,
        mds.STATUS_CHANGES,
        engine,
        table,
        before_load=before_load,
        stage_first=stage_first
    )

def load_trips(sources, engine, table=mds.TRIPS, stage_first=True):
    """
    Load trips data from :sources: using the connection defined by :engine:.

    By default, stages the load into a temp table before upserting to the final destination.
    """
    def before_load(df):
        _json_cols_tostring(df, ["route"])
        return _add_missing_cols(df, ["parking_verification_url", "standard_cost", "actual_cost"])

    load_from_source(
        sources,
        mds.TRIPS,
        engine,
        table,
        before_load=before_load,
        stage_first=stage_first
    )

