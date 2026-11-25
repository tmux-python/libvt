# `libvt.config`

```{eval-rst}
.. automodule:: libvt.config
   :members:
   :no-private-members:
   :exclude-members: Charset, SixelErasePolicy

.. py:type:: Charset
   :module: libvt.config
   :canonical: typing.Literal['B', '0', 'A']

   Supported ``G0``–``G3`` character-set designators (US-ASCII, DEC Special,
   UK).

.. py:type:: SixelErasePolicy
   :module: libvt.config
   :canonical: typing.Literal['cell_mask', 'whole_image']

   ``SIXEL`` cell-overwrite and rectangular-erase clipping policy.
```
