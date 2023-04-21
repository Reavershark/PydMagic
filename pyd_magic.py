import sys
if sys.version_info[0] < 3:
    raise ImportError('Only python 3+ is supported.')

from IPython.core import magic_arguments
from IPython.core.magic import cell_magic, magics_class, Magics
from IPython.utils.path import get_ipython_cache_dir
from IPython.utils import py3compat
import os
import io
import time
import importlib.util
import json
import subprocess
import shutil
import ast

from mergedict import ConfigDict

try:
    import hashlib
except ImportError:
    import md5 as hashlib

from distutils.core import Distribution
from distutils.command.build_ext import build_ext

import pyd.support

@magics_class
class PydMagics(Magics):
    def __init__(self, shell):
        super(PydMagics, self).__init__(shell)
        self._reloads = {}
        self._code_cache = {}

    def _import_all(self, module):
        for k,v in module.__dict__.items():
            if not k.startswith('__'):
                self.shell.push({k:v})

    @magic_arguments.magic_arguments()
    @magic_arguments.argument(
        '-n', '--name',
        help="Specify a name for the Pyd module."
    )
    @magic_arguments.argument(
        '-I', '--include', action='append', default=[],
        help="Add a path to the list of include directories (can be specified "
             "multiple times)."
    )
    @magic_arguments.argument(
        '-f', '--force', action='store_true', default=False,
        help="Force the compilation of a new module, even if the source has been "
             "previously compiled."
    )
    @magic_arguments.argument(
        '--compiler', action='store', default='dmd',
        help="Specify the D compiler to be used. Default is dmd"
    )
    @magic_arguments.argument(
        '--compiler_type', action='store', default='dmd',
        help="Specify the compiler type, as in dmd, gdc, ldc or sdc. Needed if "
             "you are using a non-standard compiler name e.g. dmd_HEAD for your"
             "own personal build of dmd from git master HEAD"
    )
    @magic_arguments.argument(
        '--pyd_version', action='store', default='>=0.9.7',
        help="Specify the pyd version to use, as a dub version specifier "
             "see http://code.dlang.org/package-format#version-specs"
    )
    @magic_arguments.argument(
        '--dub_config', action='store', default='{}',
        help='''add your own compilation flags, dependencies etc. as json '''
             '''to be merged with dub.json e.g. {"libs":"fftw3"}. Note '''
             '''that spaces will be interpreted as starting a new argument, '''
             '''If you want spaces in your argument, wrap it in a string'''
    )
    @magic_arguments.argument(
        '--dub_args', action='store', default='',
        help="command line flags to dub. wrap in a string if you want to list "
             "multiple arguments."
    )
    @magic_arguments.argument(
        '--print_compiler_output', action='store_true',
        help="Print the output from the compilation process, even if compilation "
             "runs sucessfully"
    )

    @cell_magic
    def pyd(self, line, cell):
        # Read all arguments to %%pyd
        args = magic_arguments.parse_argstring(self.pyd, line)

        # Construct a single d source code file
        def construct_d_source_code() -> str:
            d_code = """
                import ppyd;

                extern(C) void PydMain()
                {
                    import std.meta : Alias;
                    registerAll!(Alias!(__traits(parent, PydMain)))();
                }

            """
            d_code += cell

            if not code.endswith('\n'):
                d_code + '\n'

            return d_code

        # Parse --dub_config
        def parse_dub_config() -> None:
            if args.dub_config:
                try:
                    args.dub_config = json.loads(args.dub_config)
                except e1:
                    try:
                        args.dub_config = json.loads(ast.literal_eval(args.dub_config))
                    except e2:
                        print('Failed to parse --dub_config:')
                        print(f'  json.loads method error: {e1}')
                        print(f'  ast.literal_eval method error: {e2}')
                        raise e2

        # Parse --dub_args
        def parse_dub_args() -> None:
            if args.dub_args:
                try:
                    args.dub_args = ast.literal_eval(args.dub_args)
                except e:
                    print("Failed to parse --dub_args: {e}")
                    raise e

        # Handle --force
        def handle_force() -> bool:
            if args.force:
                args.dub_args = '--force ' + args.dub_args
                return True
            return False

        def get_module_name_from_config_tuple(config_tuple: tuple):
            if args.name:
                return args.name
            else:
                return "_pyd_magic_" + hashlib.md5(str(tuple_unique_to_this_config).encode('utf-8')).hexdigest()

        # Used for choosing the name of the module/build folder by hashing the tuple
        tuple_unique_to_this_config = ()

        # Rebuild if the execution environment changes significantly
        tuple_unique_to_this_config += (sys.version_info, sys.executable)

        # Rebuild if the args to %%pyd change
        tuple_unique_to_this_config += (line,)

        d_code = construct_d_source_code()

        # Rebuild if the source code changes
        tuple_unique_to_this_config += (d_code,)

        parse_dub_config()
        parse_dub_args()

        if handle_force():
            # Force rebuild by appending the current time to the config tuple
            tuple_unique_to_this_config += (time.time(),)

        module_name = get_module_name_from_config_tuple(tuple_unique_to_this_config)

        # Choose and create the module dir (build folder)
        module_dir = os.path.join(get_ipython_cache_dir(), 'pyd', module_name)
        if not os.path.exists(module_dir):
            os.makedirs(module_dir)

        # Choose the path of the dynamic library to be built
        if os.name == 'nt':
            so_ext = '.dll'
        else:
            so_ext = '.so' #might have to go to dylib on OS X at some point???
        module_path = os.path.join(module_path, 'lib' + module_name + so_ext)

        was_already_built = os.path.isfile(module_path)

        if not was_already_built: # Build module
            # Unused
            d_include_dirs = args.include

            def write_source_code_file() -> None:
                pyd_file = os.path.join(module_dir, module_name + '.d')
                with open(pyd_file, 'w', encoding='utf-8') as f:
                    f.write(d_code)

            def generate_dub_json_basic() -> dict:
                pyd_dub_json = {}
                pyd_dub_json['name'] = module_name
                pyd_dub_json['dependencies'] = { "pyd": args.pyd_version, "ppyd": ">=0.1.3" }
                pyd_dub_json['subConfigurations'] = { "pyd": f"python{sys.version_info.major}{sys.version_info.minor}" }
                pyd_dub_json['sourceFiles'] = [pyd_file]
                pyd_dub_json['targetType'] = 'dynamicLibrary'
                pyd_dub_json['dflags-dmd'] = ['-fPIC']
                pyd_dub_json['dflags-ldc'] = ['--relocation-model=pic']
                pyd_dub_json['libs'] = []
                pyd_dub_json['versions'] = ['PydPythonExtension']
                return pyd_dub_json

            def write_dub_json(json: dict) -> None:
                pyd_dub_file = os.path.join(module_dir, 'dub.json')

                with open(pyd_dub_file, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(pyd_dub_json) + '\n')

            def remove_sub_selections_json() -> None:
                pyd_dub_selections_file = os.path.join(module_dir, 'dub.selections.json')

                try:
                    os.remove(pyd_dub_selections_file)
                except:
                    pass

            write_source_code_file()
            write_dub_json(generate_dub_json_basic())
            remove_sub_selections_json()

            def generate_dub_json_extended() -> dict:
                pyd_dub_json = generate_dub_json_basic()

                dub_desc = json.loads(subprocess.check_output(['dub', 'describe', f'--root={module_dir}'], universal_newlines = True))
                for package in dub_desc['packages']:
                    if package['name'] == 'pyd':
                        pyd_infrastructure_path = os.path.join(package['path'], 'infrastructure')
                        break

                if os.name == 'nt':
                    boilerplate_file_name = 'python_dll_windows_boilerplate.d'
                else:
                    boilerplate_file_name = 'python_so_linux_boilerplate.d'
                boilerplate_file_path = os.path.join(pyd_infrastructure_path, 'd', boilerplate_file_name)
                pyd_dub_json['sourceFiles'].append(boilerplatePath)

                if args.compiler == 'dmd':
                    so_ctor_path = os.path.join(pyd_infrastructure_path, 'd', 'so_ctor.c')
                    so_ctor_object_path = os.path.join(module_dir, "so_ctor.o")
                    subprocess.check_call(['cc', '-c', '-fPIC', '-o', so_ctor_object_path, so_ctor_path])
                    pyd_dub_json['sourceFiles'].append(so_ctor_object_path)

                mainTemplate = os.path.join(pyd_infrastructure_path, 'd', 'pydmain_template.d')
                mainTemplateOut = os.path.join(module_dir, 'pydmain.d')
                with open(mainTemplate, 'r', encoding='utf-8') as t, open(mainTemplateOut, 'w', encoding='utf-8') as m:
                    m.write(t.read() % {'modulename' : module_name})
                pyd_dub_json['sourceFiles'].append(mainTemplateOut)

                pyd_dub_json = ConfigDict(pyd_dub_json)
                pyd_dub_json.merge(args.dub_config)

                return pyd_dub_json

            write_dub_json(generate_dub_json_extended())

            def build_module() -> None:
                try:
                    output = subprocess.check_output(['dub', 'build', f'--root={module_dir}', *args.dub_args.split(None)],
                            universal_newlines=True, stderr=subprocess.STDOUT)
                except (subprocess.CalledProcessError) as e:
                    print(e.output)
                    raise e
                if args.print_compiler_output:
                    print(output)

            build_module()

        if not was_already_built:
            self._code_cache[key] = module_name

        # Import the module
        module_spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(module_spec)
        self._import_all(module)

def load_ipython_extension(ip):
    ip.register_magics(PydMagics)
