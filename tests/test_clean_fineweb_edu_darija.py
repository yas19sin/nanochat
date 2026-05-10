from scripts.clean_fineweb_edu_darija import (
    CleanConfig,
    check_row,
    normalize_text,
)


def test_normalize_text_strips_control_whitespace():
    text = "  hello\u00a0 world\r\n\r\n\r\n  again \x00 "
    assert normalize_text(text) == "hello world\n\nagain"


def test_check_row_accepts_good_translation():
    row = {
        "src_idx": 7,
        "en": "The teacher explained how plants grow from seeds in simple words.",
        "darija": "شرح الأستاذ كيفاش النباتات كيكبرو من الزريعة بكلمات بسيطة.",
        "output_tokens": "19",
    }

    clean, reason = check_row(row, source_ordinal=0, cfg=CleanConfig(), val_full=False)

    assert reason == "accepted"
    assert clean is not None
    assert clean.src_idx == 7
    assert clean.output_tokens == 19
    assert clean.split == "val"


def test_check_row_rejects_translator_boilerplate():
    row = {
        "en": "This lesson explains the water cycle and why clouds produce rain.",
        "darija": "Here is the Darija translation: هاد الدرس كيشرح دورة الماء وعلاش السحاب كينتج الشتا.",
    }

    clean, reason = check_row(row, source_ordinal=3, cfg=CleanConfig(), val_full=True)

    assert clean is None
    assert reason == "translator_boilerplate"


def test_check_row_can_reject_number_mismatch():
    cfg = CleanConfig(drop_number_mismatches=True)
    row = {
        "en": "The experiment used 12 samples and lasted 45 minutes in total.",
        "darija": "التجربة استعملات 13 عينة ودامت 45 دقيقة فالمجموع.",
    }

    clean, reason = check_row(row, source_ordinal=4, cfg=cfg, val_full=True)

    assert clean is None
    assert reason == "number_mismatch"
