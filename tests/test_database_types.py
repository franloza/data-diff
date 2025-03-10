from contextlib import suppress
import unittest
import time
import json
import re
import rich.progress
import math
import uuid
from datetime import datetime, timedelta, timezone
import logging
from decimal import Decimal
from parameterized import parameterized

from data_diff import databases as db
from data_diff.utils import number_to_human
from data_diff.diff_tables import TableDiffer, TableSegment, DEFAULT_BISECTION_THRESHOLD
from .common import CONN_STRINGS, N_SAMPLES, N_THREADS, BENCHMARK, GIT_REVISION, random_table_suffix


CONNS = {k: db.connect_to_uri(v, N_THREADS) for k, v in CONN_STRINGS.items()}

CONNS[db.MySQL].query("SET @@session.time_zone='+00:00'", None)


class PaginatedTable:
    # We can't query all the rows at once for large tables. It'll occupy too
    # much memory.
    RECORDS_PER_BATCH = 1000000

    def __init__(self, table, conn):
        self.table = table
        self.conn = conn

    def __iter__(self):
        iter = PaginatedTable(self.table, self.conn)
        iter.last_id = 0
        iter.values = []
        iter.value_index = 0
        return iter

    def __next__(self) -> str:
        if self.value_index == len(self.values):  #  end of current batch
            query = f"SELECT id, col FROM {self.table} WHERE id > {self.last_id} ORDER BY id ASC LIMIT {self.RECORDS_PER_BATCH}"
            if isinstance(self.conn, db.Oracle):
                query = f"SELECT id, col FROM {self.table} WHERE id > {self.last_id} ORDER BY id ASC OFFSET 0 ROWS FETCH NEXT {self.RECORDS_PER_BATCH} ROWS ONLY"

            self.values = self.conn.query(query, list)
            if len(self.values) == 0:  #  we must be done!
                raise StopIteration
            self.last_id = self.values[-1][0]
            self.value_index = 0

        this_value = self.values[self.value_index]
        self.value_index += 1
        return this_value


class DateTimeFaker:
    MANUAL_FAKES = [
        datetime.fromisoformat("2020-01-01 15:10:10"),
        datetime.fromisoformat("2020-02-01 09:09:09"),
        datetime.fromisoformat("2022-03-01 15:10:01.139"),
        datetime.fromisoformat("2022-04-01 15:10:02.020409"),
        datetime.fromisoformat("2022-05-01 15:10:03.003030"),
        datetime.fromisoformat("2022-06-01 15:10:05.009900"),
    ]

    def __init__(self, max):
        self.max = max

    def __iter__(self):
        iter = DateTimeFaker(self.max)
        iter.prev = datetime(2000, 1, 1, 0, 0, 0, 0)
        iter.i = 0
        return iter

    def __len__(self):
        return self.max

    def __next__(self) -> datetime:
        if self.i < len(self.MANUAL_FAKES):
            fake = self.MANUAL_FAKES[self.i]
            self.i += 1
            return fake
        elif self.i < self.max:
            self.prev = self.prev + timedelta(seconds=3, microseconds=571)
            self.i += 1
            return self.prev
        else:
            raise StopIteration


class IntFaker:
    MANUAL_FAKES = [127, -3, -9, 37, 15, 127]

    def __init__(self, max):
        self.max = max

    def __iter__(self):
        iter = IntFaker(self.max)
        iter.prev = -128
        iter.i = 0
        return iter

    def __len__(self):
        return self.max

    def __next__(self) -> int:
        if self.i < len(self.MANUAL_FAKES):
            fake = self.MANUAL_FAKES[self.i]
            self.i += 1
            return fake
        elif self.i < self.max:
            self.prev += 1
            self.i += 1
            return self.prev
        else:
            raise StopIteration


class FloatFaker:
    MANUAL_FAKES = [
        0.0,
        0.1,
        0.00188,
        0.99999,
        0.091919,
        0.10,
        10.0,
        100.98,
        0.001201923076923077,
        1 / 3,
        1 / 5,
        1 / 109,
        1 / 109489,
        1 / 1094893892389,
        1 / 10948938923893289,
        3.141592653589793,
    ]

    def __init__(self, max):
        self.max = max

    def __iter__(self):
        iter = FloatFaker(self.max)
        iter.prev = -10.0001
        iter.i = 0
        return iter

    def __len__(self):
        return self.max

    def __next__(self) -> float:
        if self.i < len(self.MANUAL_FAKES):
            fake = self.MANUAL_FAKES[self.i]
            self.i += 1
            return fake
        elif self.i < self.max:
            self.prev += 0.00571
            self.i += 1
            return self.prev
        else:
            raise StopIteration


class UUID_Faker:
    def __init__(self, max):
        self.max = max

    def __len__(self):
        return self.max

    def __iter__(self):
        return (uuid.uuid1(i) for i in range(self.max))


TYPE_SAMPLES = {
    "int": IntFaker(N_SAMPLES),
    "datetime": DateTimeFaker(N_SAMPLES),
    "float": FloatFaker(N_SAMPLES),
    "uuid": UUID_Faker(N_SAMPLES),
}

DATABASE_TYPES = {
    db.PostgreSQL: {
        # https://www.postgresql.org/docs/current/datatype-numeric.html#DATATYPE-INT
        "int": [
            # "smallint",  # 2 bytes
            "int",  # 4 bytes
            "bigint",  # 8 bytes
        ],
        # https://www.postgresql.org/docs/current/datatype-datetime.html
        "datetime": [
            "timestamp(6) without time zone",
            "timestamp(3) without time zone",
            "timestamp(0) without time zone",
            "timestamp with time zone",
        ],
        # https://www.postgresql.org/docs/current/datatype-numeric.html
        "float": [
            "real",
            "float",
            "double precision",
            "numeric(6,3)",
        ],
        "uuid": [
            "text",
            "varchar(100)",
            "char(100)",
        ],
    },
    db.MySQL: {
        # https://dev.mysql.com/doc/refman/8.0/en/integer-types.html
        "int": [
            # "tinyint", # 1 byte
            # "smallint", # 2 bytes
            # "mediumint", # 3 bytes
            "int",  # 4 bytes
            "bigint",  # 8 bytes
        ],
        # https://dev.mysql.com/doc/refman/8.0/en/datetime.html
        "datetime": [
            "timestamp(6)",
            "timestamp(3)",
            "timestamp(0)",
            "timestamp",
            "datetime(6)",
        ],
        # https://dev.mysql.com/doc/refman/8.0/en/numeric-types.html
        "float": [
            "float",
            "double",
            "numeric",
            "numeric(65, 10)",
        ],
        "uuid": [
            "varchar(100)",
            "char(100)",
            "varbinary(100)",
        ],
    },
    db.BigQuery: {
        "int": ["int"],
        "datetime": [
            "timestamp",
            "datetime",
        ],
        "float": [
            "numeric",
            "float64",
            "bignumeric",
        ],
        "uuid": [
            "STRING",
        ],
    },
    db.Snowflake: {
        # https://docs.snowflake.com/en/sql-reference/data-types-numeric.html#int-integer-bigint-smallint-tinyint-byteint
        "int": [
            # all 38 digits with 0 precision, don't need to test all
            "int",
            "bigint",
            # "smallint",
            # "tinyint",
            # "byteint"
        ],
        # https://docs.snowflake.com/en/sql-reference/data-types-datetime.html
        "datetime": [
            "timestamp(0)",
            "timestamp(3)",
            "timestamp(6)",
            "timestamp(9)",
            "timestamp_tz(9)",
            "timestamp_ntz(9)",
        ],
        # https://docs.snowflake.com/en/sql-reference/data-types-numeric.html#decimal-numeric
        "float": [
            "float",
            "numeric",
        ],
        "uuid": [
            "varchar",
            "varchar(100)",
        ],
    },
    db.Redshift: {
        "int": [
            "int",
        ],
        "datetime": [
            "TIMESTAMP",
            "timestamp with time zone",
        ],
        # https://docs.aws.amazon.com/redshift/latest/dg/r_Numeric_types201.html#r_Numeric_types201-floating-point-types
        "float": [
            "float4",
            "float8",
            "numeric",
        ],
        "uuid": [
            "text",
            "varchar(100)",
            "char(100)",
        ],
    },
    db.Oracle: {
        "int": [
            "int",
        ],
        "datetime": [
            "timestamp with local time zone",
            "timestamp(6) with local time zone",
            "timestamp(9) with local time zone",
        ],
        "float": [
            "float",
            "numeric",
            "real",
            "double precision",
        ],
        "uuid": [
            "CHAR(100)",
            "VARCHAR(100)",
            "NCHAR(100)",
            "NVARCHAR2(100)",
        ],
    },
    db.Presto: {
        "int": [
            # "tinyint", # 1 byte
            # "smallint", # 2 bytes
            # "mediumint", # 3 bytes
            "int",  # 4 bytes
            "bigint",  # 8 bytes
        ],
        "datetime": [
            "timestamp",
            "timestamp with time zone",
        ],
        "float": [
            "real",
            "double",
            "decimal(10,2)",
            "decimal(30,6)",
        ],
        "uuid": [
            "varchar",
            "char(100)",
        ],
    },
}


type_pairs = []
for source_db, source_type_categories in DATABASE_TYPES.items():
    for target_db, target_type_categories in DATABASE_TYPES.items():
        for (
            type_category,
            source_types,
        ) in source_type_categories.items():  # int, datetime, ..
            for source_type in source_types:
                for target_type in target_type_categories[type_category]:
                    if CONNS.get(source_db, False) and CONNS.get(target_db, False):
                        type_pairs.append(
                            (
                                source_db,
                                target_db,
                                source_type,
                                target_type,
                                type_category,
                            )
                        )


def sanitize(name):
    name = name.lower()
    name = re.sub(r"[\(\)]", "", name)  #  timestamp(9) -> timestamp9
    # Try to shorten long fields, due to length limitations in some DBs
    name = name.replace(r"without time zone", "n_tz")
    name = name.replace(r"with time zone", "y_tz")
    name = name.replace(r"with local time zone", "y_tz")
    name = name.replace(r"timestamp", "ts")
    name = name.replace(r"double precision", "double")
    name = name.replace(r"numeric", "num")
    return parameterized.to_safe_name(name)


# Pass --verbose to test run to get a nice output.
def expand_params(testcase_func, param_num, param):
    source_db, target_db, source_type, target_type, type_category = param.args
    source_db_type = source_db.__name__
    target_db_type = target_db.__name__

    name = "%s_%s_%s_%s_%s_%s" % (
        testcase_func.__name__,
        sanitize(source_db_type),
        sanitize(source_type),
        sanitize(target_db_type),
        sanitize(target_type),
        number_to_human(N_SAMPLES),
    )

    return name


def _insert_to_table(conn, table, values, type):
    current_n_rows = conn.query(f"SELECT COUNT(*) FROM {table}", int)
    if current_n_rows == N_SAMPLES:
        assert BENCHMARK, "Table should've been deleted, or we should be in BENCHMARK mode"
        return
    elif current_n_rows > 0:
        _drop_table_if_exists(conn, table)
        _create_table_with_indexes(conn, table, type)

    if BENCHMARK and N_SAMPLES > 10_000:
        description = f"{conn.name}: {table}"
        values = rich.progress.track(values, total=N_SAMPLES, description=description)

    default_insertion_query = f"INSERT INTO {table} (id, col) VALUES "
    if isinstance(conn, db.Oracle):
        default_insertion_query = f"INSERT INTO {table} (id, col)"

    batch_size = 8000
    if isinstance(conn, db.BigQuery):
        batch_size = 1000

    insertion_query = default_insertion_query
    selects = []
    for j, sample in values:
        if re.search(r"(time zone|tz)", type):
            sample = sample.replace(tzinfo=timezone.utc)

        if isinstance(sample, (float, Decimal, int)):
            value = str(sample)
        elif isinstance(sample, datetime) and isinstance(conn, (db.Presto, db.Oracle)):
            value = f"timestamp '{sample}'"
        elif isinstance(sample, bytearray):
            value = f"'{sample.decode()}'"
        else:
            value = f"'{sample}'"

        if isinstance(conn, db.Oracle):
            selects.append(f"SELECT {j}, {value} FROM dual")
        else:
            insertion_query += f"({j}, {value}),"

        # Some databases want small batch sizes...
        # Need to also insert on the last row, might not divide cleanly!
        if j % batch_size == 0 or j == N_SAMPLES:
            if isinstance(conn, db.Oracle):
                insertion_query += " UNION ALL ".join(selects)
                conn.query(insertion_query, None)
                selects = []
            else:
                conn.query(insertion_query[0:-1], None)
                insertion_query = default_insertion_query

    if not isinstance(conn, db.BigQuery):
        conn.query("COMMIT", None)


def _create_indexes(conn, table):
    # It is unfortunate that Presto doesn't support creating indexes...
    # Technically we could create it in the backing Postgres behind the scenes.
    if isinstance(conn, (db.Snowflake, db.Redshift, db.Presto, db.BigQuery)):
        return

    try:
        if_not_exists = "IF NOT EXISTS" if not isinstance(conn, (db.MySQL, db.Oracle)) else ""
        conn.query(
            f"CREATE INDEX {if_not_exists} xa_{table[1:-1]} ON {table} (id, col)",
            None,
        )
        conn.query(
            f"CREATE INDEX {if_not_exists} xb_{table[1:-1]} ON {table} (id)",
            None,
        )
    except Exception as err:
        if "Duplicate key name" in str(err):  #  mysql
            pass
        elif "such column list already indexed" in str(err):  #  oracle
            pass
        elif "name is already used" in str(err):  #  oracle
            pass
        else:
            raise (err)


def _create_table_with_indexes(conn, table, type):
    if isinstance(conn, db.Oracle):
        already_exists = conn.query(f"SELECT COUNT(*) from tab where tname='{table.upper()}'", int) > 0
        if not already_exists:
            conn.query(f"CREATE TABLE {table}(id int, col {type})", None)
    else:
        conn.query(f"CREATE TABLE IF NOT EXISTS {table}(id int, col {type})", None)

    _create_indexes(conn, table)
    if not isinstance(conn, db.BigQuery):
        conn.query("COMMIT", None)


def _drop_table_if_exists(conn, table):
    with suppress(db.QueryError):
        if isinstance(conn, db.Oracle):
            conn.query(f"DROP TABLE {table}", None)
            conn.query(f"DROP TABLE {table}", None)
        else:
            conn.query(f"DROP TABLE IF EXISTS {table}", None)
            if not isinstance(conn, db.BigQuery):
                conn.query("COMMIT", None)


class TestDiffCrossDatabaseTables(unittest.TestCase):
    maxDiff = 10000

    def tearDown(self) -> None:
        if not BENCHMARK:
            _drop_table_if_exists(self.src_conn, self.src_table)
            _drop_table_if_exists(self.dst_conn, self.dst_table)

        return super().tearDown()

    @parameterized.expand(type_pairs, name_func=expand_params)
    def test_types(self, source_db, target_db, source_type, target_type, type_category):
        start = time.time()

        self.src_conn = src_conn = CONNS[source_db]
        self.dst_conn = dst_conn = CONNS[target_db]

        self.connections = [self.src_conn, self.dst_conn]
        sample_values = TYPE_SAMPLES[type_category]

        table_suffix = ""
        # Benchmarks we re-use tables for performance. For tests, we create
        # unique tables to ensure isolation.
        if not BENCHMARK:
            table_suffix = random_table_suffix()

        # Limit in MySQL is 64, Presto seems to be 63
        src_table_name = f"src_{self._testMethodName[11:]}{table_suffix}"
        dst_table_name = f"dst_{self._testMethodName[11:]}{table_suffix}"

        src_table_path = src_conn.parse_table_name(src_table_name)
        dst_table_path = dst_conn.parse_table_name(dst_table_name)
        self.src_table = src_table = src_conn.quote(".".join(src_table_path))
        self.dst_table = dst_table = dst_table = dst_conn.quote(".".join(dst_table_path))

        start = time.time()
        if not BENCHMARK:
            _drop_table_if_exists(src_conn, src_table)
        _create_table_with_indexes(src_conn, src_table, source_type)
        _insert_to_table(src_conn, src_table, enumerate(sample_values, 1), source_type)
        insertion_source_duration = time.time() - start

        values_in_source = PaginatedTable(src_table, src_conn)
        if source_db is db.Presto:
            if source_type.startswith("decimal"):
                values_in_source = ((a, Decimal(b)) for a, b in values_in_source)
            elif source_type.startswith("timestamp"):
                values_in_source = ((a, datetime.fromisoformat(b.rstrip(" UTC"))) for a, b in values_in_source)

        start = time.time()
        if not BENCHMARK:
            _drop_table_if_exists(dst_conn, dst_table)
        _create_table_with_indexes(dst_conn, dst_table, target_type)
        _insert_to_table(dst_conn, dst_table, values_in_source, target_type)
        insertion_target_duration = time.time() - start

        if type_category == "uuid":
            self.table = TableSegment(self.src_conn, src_table_path, "col", None, ("id",), case_sensitive=False)
            self.table2 = TableSegment(self.dst_conn, dst_table_path, "col", None, ("id",), case_sensitive=False)
        else:
            self.table = TableSegment(self.src_conn, src_table_path, "id", None, ("col",), case_sensitive=False)
            self.table2 = TableSegment(self.dst_conn, dst_table_path, "id", None, ("col",), case_sensitive=False)

        start = time.time()
        self.assertEqual(N_SAMPLES, self.table.count())
        count_source_duration = time.time() - start

        start = time.time()
        self.assertEqual(N_SAMPLES, self.table2.count())
        count_target_duration = time.time() - start

        # When testing, we configure these to their lowest possible values for
        # the DEFAULT_N_SAMPLES.
        # When benchmarking, we try to dynamically create some more optimal
        # configuration with each segment being ~250k rows.
        ch_factor = min(max(int(N_SAMPLES / 250_000), 2), 128) if BENCHMARK else 2
        ch_threshold = min(DEFAULT_BISECTION_THRESHOLD, int(N_SAMPLES / ch_factor)) if BENCHMARK else 3
        ch_threads = N_THREADS
        differ = TableDiffer(
            bisection_threshold=ch_threshold,
            bisection_factor=ch_factor,
            max_threadpool_size=ch_threads,
        )
        start = time.time()
        diff = list(differ.diff_tables(self.table, self.table2))
        checksum_duration = time.time() - start
        expected = []
        self.assertEqual(expected, diff)
        self.assertEqual(0, differ.stats.get("rows_downloaded", 0))

        # This section downloads all rows to ensure that Python agrees with the
        # database, in terms of comparison.
        #
        # For benchmarking, to make it fair, we split into segments of a
        # reasonable amount of rows each. These will then be downloaded in
        # parallel, using the existing implementation.
        dl_factor = max(int(N_SAMPLES / 100_000), 2) if BENCHMARK else 2
        dl_threshold = int(N_SAMPLES / dl_factor) + 1 if BENCHMARK else math.inf
        dl_threads = N_THREADS
        differ = TableDiffer(
            bisection_threshold=dl_threshold, bisection_factor=dl_factor, max_threadpool_size=dl_threads
        )
        start = time.time()
        diff = list(differ.diff_tables(self.table, self.table2))
        download_duration = time.time() - start
        expected = []
        self.assertEqual(expected, diff)
        self.assertEqual(len(sample_values), differ.stats.get("rows_downloaded", 0))

        result = {
            "test": self._testMethodName,
            "source_db": source_db.__name__,
            "target_db": target_db.__name__,
            "date": str(datetime.today()),
            "git_revision": GIT_REVISION,
            "rows": N_SAMPLES,
            "rows_human": number_to_human(N_SAMPLES),
            "name_human": f"{source_db.__name__}/{sanitize(source_type)} <-> {target_db.__name__}/{sanitize(target_type)}",
            "src_table": src_table[1:-1],  #  remove quotes
            "target_table": dst_table[1:-1],
            "source_type": source_type,
            "target_type": target_type,
            "insertion_source_sec": round(insertion_source_duration, 3),
            "insertion_target_sec": round(insertion_target_duration, 3),
            "count_source_sec": round(count_source_duration, 3),
            "count_target_sec": round(count_target_duration, 3),
            "count_max_sec": max(round(count_target_duration, 3), round(count_source_duration, 3)),
            "checksum_sec": round(checksum_duration, 3),
            "download_sec": round(download_duration, 3),
            "download_bisection_factor": dl_factor,
            "download_bisection_threshold": dl_threshold,
            "download_threads": dl_threads,
            "checksum_bisection_factor": ch_factor,
            "checksum_bisection_threshold": ch_threshold,
            "checksum_threads": ch_threads,
        }

        if BENCHMARK:
            print(json.dumps(result, indent=2))
            file_name = f"benchmark_{GIT_REVISION}.jsonl"
            with open(file_name, "a", encoding="utf-8") as file:
                file.write(json.dumps(result) + "\n")
                file.flush()
            print(f"Written to {file_name}")
        else:
            logging.debug(json.dumps(result, indent=2))
