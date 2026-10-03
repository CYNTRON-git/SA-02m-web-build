# -*- coding: utf-8 -*-
"""OpenAPI 3.1 document covers every registered route."""

import unittest

from sa02m_agent_api.openapi import build
from sa02m_agent_api.ops import register_all
from sa02m_agent_api.registry import OPS


class OpenApiValid(unittest.TestCase):
    def test_document(self):
        register_all()
        doc = build(OPS)
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertIn("info", doc)
        self.assertIn("paths", doc)
        self.assertGreater(len(OPS), 40)
        for op in OPS:
            node = doc["paths"].get(op.rest_path)
            self.assertIsNotNone(node, op.name)
            post = node.get("post")
            self.assertEqual(post.get("operationId"), op.name)
            self.assertEqual(post.get("x-sa02m-scope"), op.scope)
            self.assertIn("200", post.get("responses") or {})
        health = doc["paths"]["/health"]["get"]
        self.assertEqual(health["operationId"], "health")
        self.assertEqual(health["security"], [])


if __name__ == "__main__":
    unittest.main()
