import unittest

from github_api import GitHubApiError, repository_name


class RepositoryNameTests(unittest.TestCase):
    def test_parses_https_repository(self) -> None:
        self.assertEqual(
            repository_name("https://github.com/example/project.git"),
            "example/project",
        )

    def test_rejects_non_github_repository(self) -> None:
        with self.assertRaises(GitHubApiError):
            repository_name("https://example.com/project.git")


if __name__ == "__main__":
    unittest.main()
