# -*- mode: python -*-

block_cipher = None

a = Analysis(['user_sync/app.py'],
             binaries=[],
             datas=[
                    ('user_sync/resources/*.cfg', 'resources'),
                    ('user_sync/resources/manual_url', 'resources'),
                    ('user_sync/resources/README.md', 'resources'),
                    ('user_sync/resources/examples/config files - basic/*', 'resources/examples/config files - basic'),
                    ('user_sync/resources/examples/config files - custom attributes and mappings/*', 'resources/examples/config files - custom attributes and mappings'),
                    ('user_sync/resources/examples/sign/*', 'resources/examples/sign'),
                    ('user_sync/resources/examples/csv inputs - user and remove lists/*', 'resources/examples/csv inputs - user and remove lists'),
                    ('user_sync/resources/shell_scripts/win', 'resources/shell_scripts/win'),
                    ('user_sync/resources/shell_scripts/linux', 'resources/shell_scripts/linux'),
              ],
             hiddenimports=['win32timezone', 'pkg_resources.py2_warn', 'keyring'],
             hookspath=['.build'],
             runtime_hooks=[],
             excludes=[],
             win_no_prefer_redirects=False,
             win_private_assemblies=False,
             cipher=block_cipher,
             noarchive=False)
pyz = PYZ(a.pure, a.zipped_data,
          cipher=block_cipher)
exe = EXE(pyz,
          a.scripts,
          a.binaries,
          a.zipfiles,
          a.datas,
          [],
          name='user-sync',
          debug=False,
          bootloader_ignore_signals=False,
          strip=False,
          upx=True,
          runtime_tmpdir=None,
          console=True, icon='.build/usticon.ico')
