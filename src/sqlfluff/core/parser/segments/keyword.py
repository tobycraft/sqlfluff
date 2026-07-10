"""The KeywordSegment class."""

from typing import Optional

from sqlfluff.core.parser.segments.base import SourceFix
from sqlfluff.core.parser.segments.common import WordSegment


class KeywordSegment(WordSegment):
    """A segment used for matching single words.

    We rename the segment class here so that descendants of
    _ProtoKeywordSegment can use the same functionality
    but don't end up being labelled as a `keyword` later.
    """

    type = "keyword"

    # NOTE: No __init__ override. A previous pure-forwarding __init__ (a
    # strict subset of RawSegment.__init__'s parameters with the same
    # defaults) added a Python call frame to every keyword instantiation -
    # one of the highest-volume constructions during parsing.

    def edit(
        self, raw: Optional[str] = None, source_fixes: Optional[list[SourceFix]] = None
    ) -> "KeywordSegment":
        """Create a new segment, with exactly the same position but different content.

        Returns:
            A copy of this object with new contents.

        Used mostly by fixes.

        NOTE: This *doesn't* copy the uuid. The edited segment is a new segment.

        """
        return self.__class__(
            raw=raw or self.raw,
            pos_marker=self.pos_marker,
            instance_types=self.instance_types,
            source_fixes=source_fixes or self.source_fixes,
        )


class LiteralKeywordSegment(KeywordSegment):
    """A keyword style literal segment.

    This should be used for things like NULL, NAN, TRUE & FALSE.

    Defined here for type inheritance.
    """

    type = "literal"
