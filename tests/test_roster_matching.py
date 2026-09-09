"""Exact pasted Kakao rosters must never invent identities or approvals."""
import unittest
from copy import deepcopy

from roly.roster_ui import match_roster_text


class RosterMatchingTests(unittest.TestCase):
    def setUp(self):
        self.members = [
            {"id": 1, "riot_id": "한 글#KR1", "status": "APPROVED"},
            {"id": 2, "riot_id": "한 글#KR2", "status": "APPROVED"},
            {"id": 3, "riot_id": "대기회원#KR1", "status": "PENDING"},
            {"id": 4, "riot_id": "탈퇴회원#KR1", "status": "KICKED"},
        ]

    def test_exact_casefold_tag_identity_and_explicit_nonmatches(self):
        result = match_roster_text(self.members, "  한 글 # kr1 \n한 글#KR1\n한 글#KR2\n한글#KR1\n대기회원#KR1\n탈퇴회원#KR1\n한 글\n\n")
        self.assertEqual(result["member_ids"], [1, 2])
        self.assertEqual([row["status"] for row in result["rows"]],
                         ["MATCHED", "DUPLICATE", "MATCHED", "UNMATCHED", "UNAPPROVED", "UNAPPROVED", "INVALID"])
        self.assertTrue(all(row["member_id"] is None for row in result["rows"] if row["status"] != "MATCHED"))
        self.assertEqual(self.members[2]["status"], "PENDING")

    def test_conflicting_registry_invalid_text_and_limits_are_not_guessed(self):
        result = match_roster_text(self.members + [{"id": 5, "riot_id": "한 글#kr1", "status": "APPROVED"}], "한 글#KR1\n a#b#c \n@everyone\n")
        self.assertEqual(result["member_ids"], [])
        self.assertEqual([row["status"] for row in result["rows"]], ["AMBIGUOUS", "INVALID", "INVALID"])
        for raw in ("x" * 20001, "x#y\n" * 201, None):
            with self.assertRaises(ValueError):
                match_roster_text(self.members, raw)
        self.assertEqual(match_roster_text(self.members, " \n\n"), {"rows": [], "member_ids": []})

    def test_numbered_kakao_samples_extract_ids_only_and_preserve_leading_dot(self):
        text = """1. 겨울#kr99 / d2 / ad mid
2. Kging#kr1 / M / mid jg top
3. 팬더가서자# kr1/p/미드 탑
4. 슬모띵#kr1/s/서폿
5. 마아먕고로룡#123/서폿 미드탑정글
6. 야동초등학교#kr1/sup
7. 메이쥐#kr0 / p2 / 미드서폿
8. .경먀#kr1/d2/정글
9. 남자는티오피#kr1/d4/탑정글
10. 콩이바람이아빠#KR1/E2/정미원섶(21:15분 접속가능)"""
        ids = ["겨울#KR99", "Kging#KR1", "팬더가서자#KR1", "슬모띵#KR1", "마아먕고로룡#123",
               "야동초등학교#KR1", "메이쥐#KR0", ".경먀#KR1", "남자는티오피#KR1", "콩이바람이아빠#KR1"]
        members = [{"id": i + 1, "riot_id": riot, "status": "APPROVED", "main_role": "TOP",
                    "current_tier": "실버 4", "score": 100} for i, riot in enumerate(ids)]
        before = deepcopy(members)
        result = match_roster_text(members, text)
        self.assertEqual(result["member_ids"], list(range(1, 11)))
        self.assertEqual([row["extracted_id"] for row in result["rows"]], ids)
        self.assertEqual([row["input"] for row in result["rows"]], text.splitlines())
        self.assertTrue(all(row["status"] == "MATCHED" and row["reason"] for row in result["rows"]))
        self.assertEqual(members, before)

    def test_parenthesized_numbers_plain_ids_duplicates_and_ambiguous_numeric_names(self):
        members = self.members + [{"id": 5, "riot_id": ".앞점#KR1", "status": "APPROVED"},
                                  {"id": 6, "riot_id": "123.닉네임#KR1", "status": "APPROVED"}]
        text = "1) 한 글 # kr1 / d2 / ad\n2.한 글#KR1 / m / mid\n3).앞점#kr1 / d2\n123.닉네임#KR1\n4. 대기회원#kr1/d2\n5. 탈퇴회원#kr1/sup\n6. 없는회원#kr1 / top\n7. 한 글 / top"
        result = match_roster_text(members, text)
        self.assertEqual(result["member_ids"], [1, 5, 6])
        self.assertEqual([row["status"] for row in result["rows"]],
            ["MATCHED", "DUPLICATE", "MATCHED", "MATCHED", "UNAPPROVED", "UNAPPROVED", "UNMATCHED", "INVALID"])
        self.assertEqual(result["rows"][2]["extracted_id"], ".앞점#KR1")
        self.assertEqual(result["rows"][3]["extracted_id"], "123.닉네임#KR1")
        conflict = members + [{"id": 7, "riot_id": "닉네임#KR1", "status": "APPROVED"}]
        ambiguous = match_roster_text(conflict, "123.닉네임#KR1")
        self.assertEqual(ambiguous["member_ids"], [])
        self.assertEqual(ambiguous["rows"][0]["status"], "AMBIGUOUS")
        self.assertIn("모두 등록", ambiguous["rows"][0]["reason"])

    def test_twenty_thirty_forty_rows_and_maximum_length_nickname(self):
        for count in (20, 30, 40):
            with self.subTest(count=count):
                members = [{"id": i + 1, "riot_id": f"회원{i + 1}#KR1", "status": "APPROVED"} for i in range(count)]
                text = "\n".join(f"{i + 1}. 회원{i + 1}# kr1 / d2 / 정글 미드" for i in range(count))
                self.assertEqual(match_roster_text(members, text)["member_ids"], list(range(1, count + 1)))
        name = "가" * 40 + "#KR1"
        result = match_roster_text([{"id": 1, "riot_id": name}], "40. " + name + " / d2")
        self.assertEqual(result["member_ids"], [1])


if __name__ == "__main__":
    unittest.main()
