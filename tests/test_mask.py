from blasphemy_killer.cli import _mask


def test_mask_multiword():
    assert _mask("Jesus Christ") == "J**** C*****"


def test_mask_single_word():
    assert _mask("goddamn") == "g******"


def test_mask_keeps_punctuation_shape():
    assert _mask(" God-damn,") == " G**-d***,"


def test_mask_apostrophes_hidden():
    assert _mask("christ's sake") == "c******* s***"


def test_mask_empty():
    assert _mask("") == ""
