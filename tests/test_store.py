"""Tests for `store`'s alias keying (Task 6: address is the real identity,
not the display name -- see `set_alias`'s docstring) and its migration path
from the old name-keyed schema.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

from uagents_trace.store import get_alias_map, init_db, list_aliases, set_alias


class AliasKeyingTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        asyncio.run(init_db(self.db_path))

    def tearDown(self):
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_re_aliasing_an_address_renames_it_in_place(self):
        asyncio.run(set_alias(self.db_path, "Orchestrator", "addr-gateway"))
        warning = asyncio.run(set_alias(self.db_path, "Gateway", "addr-gateway"))
        self.assertIsNone(warning)

        alias_map = asyncio.run(get_alias_map(self.db_path))
        self.assertEqual(alias_map["addr-gateway"], "Gateway")
        self.assertEqual(len(asyncio.run(list_aliases(self.db_path))), 1)

    def test_reusing_a_name_for_a_different_address_does_not_orphan_the_first(self):
        # The bug this fixes: setting "Orchestrator" on gateway's address,
        # then later using the same name for the real orchestrator, used to
        # silently delete gateway's alias entirely (name was the primary
        # key). Now address is the key, so both rows survive -- the caller
        # just gets a warning about the shared name.
        asyncio.run(set_alias(self.db_path, "Orchestrator", "addr-gateway"))
        warning = asyncio.run(set_alias(self.db_path, "Orchestrator", "addr-orchestrator"))

        self.assertIsNotNone(warning)
        self.assertIn("addr-gateway", warning)

        alias_map = asyncio.run(get_alias_map(self.db_path))
        self.assertEqual(alias_map["addr-gateway"], "Orchestrator")
        self.assertEqual(alias_map["addr-orchestrator"], "Orchestrator")

    def test_migrates_old_name_keyed_schema_preserving_data(self):
        # Simulate a database created before this change: `aliases(name
        # PRIMARY KEY, address UNIQUE)`.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE aliases")
        conn.execute("CREATE TABLE aliases (name TEXT PRIMARY KEY, address TEXT NOT NULL UNIQUE)")
        conn.execute("INSERT INTO aliases (name, address) VALUES ('Gateway', 'addr-gateway')")
        conn.commit()
        conn.close()

        asyncio.run(init_db(self.db_path))  # re-running init_db should migrate, not error

        alias_map = asyncio.run(get_alias_map(self.db_path))
        self.assertEqual(alias_map["addr-gateway"], "Gateway")

        # And the new address-keyed upsert semantics apply from here on.
        asyncio.run(set_alias(self.db_path, "GatewayRenamed", "addr-gateway"))
        alias_map = asyncio.run(get_alias_map(self.db_path))
        self.assertEqual(alias_map["addr-gateway"], "GatewayRenamed")
        self.assertEqual(len(asyncio.run(list_aliases(self.db_path))), 1)


if __name__ == "__main__":
    unittest.main()
