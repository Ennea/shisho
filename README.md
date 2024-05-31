# shisho
司書

An opinionated AniDB rename utility written in Python.

## How to use

`ed2k` (https://github.com/Ennea/ed2k) is required; I've written it for use with shisho specifically. It's just a tiny utility that calculates the ed2k hash (used by AniDB alongside file size to identify files) of anything fed into stdin. shisho expects `ed2k` to be somewhere in your `PATH`.

### Renaming a single file

```
shisho.py path/to/file.mkv
```

### Renaming all files in a folder

This is not recursive.

```
shisho.py path/to/folder
```

### Dry-run

```
shisho.py -d path/to/file/or/folder
```

### Printing help

```
shisho.py -h
```

## FAQ

### What's opinionated about shisho?

There is no config, and as such the format used to rename files is fixed. It is like that because I've written shisho only for myself, but I'm still releasing it in case it may be useful for somebody else. The format is:
```
{romaji anime name} - {episode number} - {episode name} [{group name}]
```

### Where does shisho store its data, and what data does it store?

When you run shisho for the first time (or with the `--prompt-login` flag) it will ask you your AniDB login. Any data retrieved from the AniDB API is also stored (and used in subsequent runs). Both are written to an SQLite database which is saved in `$XDG_DATA_HOME/shisho` or `~/.local/share/shisho` (if `XDG_DATA_HOME` is not set).
