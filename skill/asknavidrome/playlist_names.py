import logging
import re
from typing import Union

DEFAULT_NAME_PATTERN = r'^(?P<group>.+?) - (?P<name>.+?)(?:_automatic)?$'
DEFAULT_SPOKEN_FORMAT = '{name}'
DEFAULT_QUALIFIED_FORMAT = '{group} {name}'


class PlaylistNames:
    """Turn stored playlist names into what the skill should say out loud.

    AudioMuse-style playlist names carry a group prefix and sometimes a
    machine suffix (e.g. "Rock - Easy Drive Down the Highway_automatic"),
    which are useful for grouping ("what rock playlists do I have") but
    noisy when read back. A regex with named groups `group` and `name`
    splits a stored name into those parts, and format strings decide how
    each is spoken. Names the regex doesn't match are spoken as-is and
    belong to no group.
    """

    def __init__(self, name_pattern: str = DEFAULT_NAME_PATTERN,
                 spoken_format: str = DEFAULT_SPOKEN_FORMAT,
                 qualified_format: str = DEFAULT_QUALIFIED_FORMAT):
        """
        :param str name_pattern: Regex with named groups `group` and `name`
        :param str spoken_format: How to speak a grouped playlist, using {group} and {name}
        :param str qualified_format: How to speak a grouped playlist whose spoken
            form collides with another's (e.g. "Quiet Evening Reflection" in both
            Rock and Pop), using {group} and {name}
        """

        self.logger = logging.getLogger(__name__)

        self.pattern = re.compile(name_pattern)
        missing = {'group', 'name'} - set(self.pattern.groupindex)
        if missing:
            raise ValueError(f'Playlist name pattern is missing named group(s): {sorted(missing)}')

        # Fail at startup rather than on the first voice request.
        for fmt in (spoken_format, qualified_format):
            fmt.format(group='g', name='n')

        self.spoken_format = spoken_format
        self.qualified_format = qualified_format

    def parse(self, full_name: str) -> Union[tuple, None]:
        """Split a stored playlist name into (group, name), or None when it
        doesn't follow the grouped naming pattern.
        """

        match = self.pattern.match(full_name or '')
        if not match:
            return None

        return match.group('group').strip(), match.group('name').strip()

    def group_of(self, full_name: str) -> Union[str, None]:
        parsed = self.parse(full_name)

        return parsed[0] if parsed else None

    def spoken(self, full_name: str) -> str:
        parsed = self.parse(full_name)
        if not parsed:
            return full_name

        group, name = parsed

        return self.spoken_format.format(group=group, name=name)

    def qualified(self, full_name: str) -> str:
        parsed = self.parse(full_name)
        if not parsed:
            return full_name

        group, name = parsed

        return self.qualified_format.format(group=group, name=name)

    def spoken_list(self, full_names: list) -> list:
        """Spoken forms for several playlists read out together, falling back
        to the qualified form for any whose spoken form would otherwise be
        indistinguishable from another in the same list.
        """

        spoken = [self.spoken(name) for name in full_names]
        counts = {}
        for name in spoken:
            counts[name.lower()] = counts.get(name.lower(), 0) + 1

        return [
            self.qualified(full) if counts[said.lower()] > 1 else said
            for full, said in zip(full_names, spoken)
        ]

    def match_forms(self, full_name: str) -> list:
        """Every form a user might say back for this playlist: the stored
        name, its spoken form, and its qualified form.
        """

        forms = [full_name, self.spoken(full_name), self.qualified(full_name)]

        return list(dict.fromkeys(forms))

    @staticmethod
    def normalize_group(text: str) -> str:
        """Compare group names loosely, so "R and B" matches "R&B" and
        "hip hop" matches "Hip-Hop".
        """

        return re.sub(r'[^a-z0-9]', '', (text or '').lower().replace('&', 'and'))
