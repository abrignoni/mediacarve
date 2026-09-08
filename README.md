# mediacarve

Recover images and video from any stream of bytes, by signature. One file, pure
Python, standard library only. No compiler, no network, nothing to install.

It reports each file it finds as an extent, so a tool that keeps its own index
can fetch the bytes on demand, and a tool that wants files on disk can have a
folder or a zip. MIT licensed.
