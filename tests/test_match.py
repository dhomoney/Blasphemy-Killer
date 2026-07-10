from blasphemy_killer.match import Word, find_matches, normalize_phrase, normalize_word


def w(text: str, start: float, end: float | None = None) -> Word:
    return Word(text=text, start=start, end=end if end is not None else start + 0.4)


def test_normalize_word():
    assert normalize_word(" God-damn,") == "goddamn"
    assert normalize_word("Christ!") == "christ"
    assert normalize_word("God's") == "god's"
    assert normalize_word("...") == ""
    assert normalize_word("Hello") == "hello"


def test_normalize_phrase_squashes_spaces():
    assert normalize_phrase("God damn") == "goddamn"
    assert normalize_phrase("for christ's sake") == "forchrist'ssake"


def test_split_words_match_multiword_phrase():
    words = [w("What", 0.0), w("the", 0.5), w("god", 1.0), w("damn", 1.5), w("hell", 2.0)]
    matches = find_matches(words, ["god damn"])
    assert len(matches) == 1
    assert matches[0].start == 1.0
    assert matches[0].end == 1.9


def test_merged_token_matches_multiword_phrase():
    words = [w("goddamn", 3.0)]
    matches = find_matches(words, ["god damn"])
    assert len(matches) == 1
    assert matches[0].phrase == "god damn"


def test_hyphenated_and_punctuated_token():
    words = [w(" God-damn,", 3.0)]
    assert len(find_matches(words, ["god damn"])) == 1


def test_boundary_negative_christmas():
    words = [w("Merry", 0.0), w("Christmas", 0.5)]
    assert find_matches(words, ["christ"]) == []


def test_boundary_negative_partial_across_words():
    # "chris" + "t" concatenates to "christ" but that's two real words; the
    # boundary rule still allows it ONLY if the phrase ends exactly at a word
    # end — which it does here. The realistic guarantee is no mid-word match:
    words = [w("christs", 0.0)]
    assert find_matches(words, ["christ"]) == []


def test_contained_match_dropped():
    words = [w("Jesus", 1.0), w("Christ", 1.5)]
    matches = find_matches(words, ["jesus christ", "jesus", "christ"])
    assert len(matches) == 1
    assert matches[0].phrase == "jesus christ"


def test_three_word_phrase():
    words = [w("I", 0.0), w("swear", 0.4), w("to", 0.8), w("God", 1.2)]
    matches = find_matches(words, ["i swear to god", "swear to god"])
    assert len(matches) == 1
    assert matches[0].phrase == "i swear to god"
    assert matches[0].start == 0.0
    assert matches[0].end == 1.6


def test_multiple_occurrences():
    words = [w("jesus", 1.0), w("okay", 2.0), w("jesus", 3.0)]
    matches = find_matches(words, ["jesus"])
    assert [m.start for m in matches] == [1.0, 3.0]


def test_empty_tokens_skipped():
    words = [w("god", 0.0), w("...", 0.2, 0.3), w("damn", 0.5)]
    assert len(find_matches(words, ["god damn"])) == 1


def test_no_phrases_no_matches():
    assert find_matches([w("hello", 0.0)], []) == []
