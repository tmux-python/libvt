# `libvt.parser`

```{eval-rst}
.. automodule:: libvt.parser
   :members:
   :no-private-members:
   :exclude-members: Action, ParserEncoding

.. py:type:: ParserEncoding
   :module: libvt.parser
   :canonical: typing.Literal['utf-8', 'latin-1']

   Wire input byte encoding accepted by :class:`Parser`.

.. py:type:: Action
   :module: libvt.parser
   :canonical: Print | Execute | EscDispatch | CsiDispatch | OscDispatch | DcsDispatch | ApcDispatch

   Closed union of parser actions emitted by :meth:`Parser.feed`.
```
