import unittest

from server import request_cache_key


class CacheKeyTests(unittest.TestCase):
    def test_same_request_has_same_key(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        self.assertEqual(
            request_cache_key("grok-4.6", messages),
            request_cache_key("grok-4.6", messages),
        )

    def test_model_is_part_of_key(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        self.assertNotEqual(
            request_cache_key("grok-4.6", messages),
            request_cache_key("another-model", messages),
        )

    def test_messages_are_part_of_key(self) -> None:
        self.assertNotEqual(
            request_cache_key(
                "grok-4.6", [{"role": "user", "content": "hello"}]
            ),
            request_cache_key(
                "grok-4.6", [{"role": "user", "content": "goodbye"}]
            ),
        )

    def test_message_order_is_part_of_key(self) -> None:
        first = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
        reversed_messages = list(reversed(first))
        self.assertNotEqual(
            request_cache_key("grok-4.6", first),
            request_cache_key("grok-4.6", reversed_messages),
        )


if __name__ == "__main__":
    unittest.main()
