import subprocess as sub
import jinja2
import re
import pandas as pd
import numpy as np
from .grammar import madx_syntax
from ..simulator import Simulator
from ..simulator import SimulatorException

SUPPORTED_PROPERTIES = ['APERTYPE',
                        'E1',
                        'E2',
                        'FINT',
                        'HGAP',
                        'THICK',
                        'TILT',
                        'K1',
                        'K2',
                        'K3',
                        'K1S',
                        'K2S',
                        'K3S',
                        ]

SUPPORTED_CLASSES = ['QUADRUPOLE',
                     'RBEND',
                     'SBEND',
                     'SEXTUPOLE',
                     'OCTUPOLE',
                     'MARKER',
                     'COLLIMATOR',
                     'INSTRUMENT',
                     ]


class MadxException(Exception):
    """Exception raised for errors in the Madx module."""

    def __init__(self, m):
        self.message = m


def element_to_mad(e):
    """Convert a pandas.Series representation onto a MAD-X sequence element."""
    if e.CLASS not in SUPPORTED_CLASSES:
        return ""
    mad = "{}: {}, ".format(e.name, e.CLASS)
    if e.get('BENDING_ANGLE') is not None and not np.isnan(e['BENDING_ANGLE']):
        mad += f"ANGLE={e['BENDING_ANGLE']},"
    elif e.get('ANGLE') is not None and not np.isnan(e['ANGLE']):
        mad += f"ANGLE={e.get('ANGLE', 0)},"
    else:
        # Angle property not supported by the element or absent
        mad += ""
    mad += ', '.join(["{}={}".format(p, e[p]) for p in SUPPORTED_PROPERTIES if pd.notnull(e.get(p, None))])
    if pd.notnull(e['LENGTH']) and e['LENGTH'] != 0.0:
        mad += ", L={}".format(e['LENGTH'])
    if pd.notnull(e.get('APERTYPE', None)):
        mad += ", APERTURE={}".format(str(e['APERTURE']).strip('[]'))
    if pd.notnull(e.get('PLUG')) and pd.notnull(e.get('CIRCUIT')) and pd.isnull(e.get('VALUE')):
        mad += ", {}:={}".format(e['PLUG'], e['CIRCUIT'])
    if pd.notnull(e.get('PLUG')) and pd.notnull(e.get('VALUE')):
        mad += ", {}={}".format(e['PLUG'], e['VALUE'])
    mad += ", AT={}".format(e['AT_CENTER'])
    mad += ";"
    return mad


def sequence_to_mad(sequence):
    """Convert a pandas.DataFrame sequence onto a MAD-X input."""
    sequence.sort_values(by='AT_CENTER', inplace=True)
    if sequence is None:
        return ""
    m = "{}: SEQUENCE, L={}, REFER=CENTER;\n".format(sequence.name, sequence.length)
    m += '\n'.join(sequence.apply(element_to_mad, axis=1)) + '\n'
    m += "ENDSEQUENCE;\n"
    if 'CIRCUIT' in sequence:
        m += '\n'.join(sequence['CIRCUIT'].dropna().map(lambda c: "{}:={{{{ {} or '0.0' }}}};".format(c, c)))
        m += '\n'
    return m


class Madx(Simulator):
    """A Python wrapper around the MAD-X executable.

    Sequence and command will be converted with the MAD-X grammar and pipe'd to the subprocess.
    """

    EXECUTABLE_NAME = 'madx'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._syntax = madx_syntax
        self._exec = Madx.EXECUTABLE_NAME

    def _attach(self, beamline):
        super()._attach(beamline)
        if beamline.length is None or pd.isnull(beamline.length):
            raise SimulatorException("Beamline length not defined.")
        self._input += sequence_to_mad(beamline.line)

    def run(self, **kwargs):
        """Run madx as a subprocess."""
        self._input += self._syntax['stop']
        template_input = jinja2.Template(self._input).render(kwargs.get("context", {}))
        if kwargs.get("debug", False) >= 2:
            print(template_input)
        if self._get_exec() is None:
            raise MadxException("Can't run MADX if no valid path and executable are defined.")
        p = sub.Popen([self._get_exec()],
                      stdin=sub.PIPE,
                      stdout=sub.PIPE,
                      stderr=sub.STDOUT,
                      cwd=".",
                      shell=True
                      )
        self._output = p.communicate(input=template_input.encode())[0].decode()
        self._warnings = [line for line in self._output.split('\n') if re.search('warning|fatal', line)]
        self._fatals = [line for line in self._output.split('\n') if re.search('fatal', line)]
        self._last_context = kwargs.get("context", {})
        if kwargs.get('debug', False):
            print(self._output)
        return self

    def raw(self, raw):
        """Add a raw MAD-X command to the input."""
        self._input += raw + "\n"
        return self

    def select_columns(self, flag, columns):
        """Add a MAD-X `select` command."""
        self._add_input('select_columns', (flag, *columns))
        return self

    def call_file(self, file):
        """Add a MAD-X `call` command."""
        self._add_input('call_file', (file,))
        return self

    def beam(self, line_name):
        """Add a MAD-X `beam` command."""
        self._add_input('beam')
        self.use_sequence(line_name)
        return self

    def use_sequence(self, sequence):
        """Add a MAD-X `use sequence` command."""
        self._add_input('use_sequence', sequence)
        return self

    def rbarc(self):
        """Add a (legacy) MAD-X `rbarc` option."""
        self._add_input('rbarc')
        return self

    def show_beam(self):
        """Add a MAD-X `show beam` command."""
        self._add_input('show_beam')
        return self

    def save_beta(self, **kwargs):
        """Add a MAD-X `save beta` command."""
        self._add_input('save_beta', kwargs.get("name", "BETA0"), kwargs.get("place", "#s"))
        return self

    def stop(self):
        """Add a MAD-X `stop` command (useful to insert as a `break point`)."""
        self._add_input('stop')

    def makethin(self, sequence, **kwargs):
        """Add a MAD-X `makethin` command."""
        style = kwargs.get('style', 'TEAPOT')
        dipole_slices = kwargs.get('dipole_slices', 4)
        quadrupole_slices = kwargs.get('quadrupole_slices', 4)
        self._input += "SELECT, FLAG=makethin, CLASS=quadrupole, THICK=false, SLICE={};\n".format(quadrupole_slices)
        self._input += "SELECT, FLAG=makethin, CLASS=rbend, THICK=false, SLICE={};\n".format(dipole_slices)
        self._add_input('makethin', sequence, style)
        self.use_sequence(sequence)
        return self

    def survey(self, **kwargs):
        """Add a MAD-X `survey` command."""
        if kwargs.get("start"):
            self.raw("SEQEDIT, SEQUENCE={};".format("BEAMLINE"))
            self.raw("CYCLE, START={};".format(kwargs.get("start")))
            self.raw("ENDEDIT;")
            self.raw("USE, SEQUENCE={};".format("BEAMLINE"))

        self._add_input("survey")

    def sectormap(self, **kwargs):
        if kwargs.get('line') is None:
            raise MadxException("A beamline must be provided.")

        if kwargs.get("start"):
            self.raw("SEQEDIT, SEQUENCE={};".format(kwargs.get('name')))
            if kwargs.get("reflect"):
                self.raw("FLATTEN;")
                self.raw("REFLECT;")
            self.raw("CYCLE, START={};".format(kwargs.get("start")))
            self.raw("ENDEDIT;")
            self.raw("USE, SEQUENCE={};".format(kwargs.get('name')))

        for p in kwargs.get("places"):
            self.raw("SELECT, FLAG=sectormap, range='{}';".format(p))
        options = ""
        for k, v in kwargs.items():
            if k not in ['ptc', 'start', 'places', 'name', 'line', 'reflect']:
                options += ",%s=%s" % (k, v)
        self._add_input('twiss_beamline', kwargs.get('file', 'twiss.outx'), options)
        return self

    def twiss(self, **kwargs):
        """Add a (ptc) `twiss` MAD-X command."""
        if kwargs.get('misalignment', False):
            self.misalign(self._beamlines)
        if kwargs.get('ptc'):
            self.__ptc_twiss(**kwargs)
        else:
            self.__madx_twiss(**kwargs)

    def __madx_twiss(self, **kwargs):
        if kwargs.get('line') is None:
            raise MadxException("A beamline must be provided.")

        if kwargs.get("start"):
            self.raw("SEQEDIT, SEQUENCE={};".format(kwargs.get('line').name))
            self.raw("CYCLE, START={};".format(kwargs.get("start")))
            self.raw("ENDEDIT;")
            self.raw("USE, SEQUENCE={};".format(kwargs.get('line').name))

        self.raw("SELECT, FLAG=sectormap, range='Q3E';")
        self.raw("SELECT, FLAG=sectormap, range='P2E';")
        options = ""
        for k, v in kwargs.items():
            if k not in ['ptc', 'start', 'line']:
                options += ",%s=%s" % (k,v)
        self._add_input('twiss_beamline', kwargs.get('file', 'twiss.outx'), options)
        return self

    def __ptc_twiss(self, **kwargs):
        if kwargs.get("fringe"):
            self.raw("PTC_SETSWITCH, FRINGE=True;")
        self._add_input('ptc_create_universe')
        self._add_input('ptc_create_layout',
                         False, 1, 6, 5, True, kwargs.get('fringe', False))
        if kwargs.get('misalignment', False):
            self._add_input('ptc_misalign')
        if kwargs.get('line', False):
            self._add_input('ptc_twiss', kwargs.get('file', 'ptc_twiss.outx'),)
        else:
            self._add_input('ptc_twiss_beamline', kwargs.get('file', 'ptc_twiss.outx'),)

        self._add_input('ptc_end')

    def __add_particles_for_tracking(self, particles, ptc=False):
        if {'X', 'PX', 'Y', 'PY', 'DPP'} > set(particles):
            return
        for r in particles.iterrows():
            if ptc:
                self._add_input('ptc_start', r[1])
            else:
                self._add_input('start_particle', r[1])

    def __generate_observation_points(self, e, length):
        if not e['AT_EXIT'] == length:
            self._add_input('observe', e.name)

    def __generate_observation_points_ptc(self, e, length):
        if not e['AT_EXIT'] == length and e['CLASS'] == 'MARKER':
            self._add_input('ptc_observe', e.name)

    def misalign(self, beamline,**kwargs):
        self._add_input('misalign_option')
        self.__add_misalignment_element(beamline)
        self.raw("USE, SEQUENCE={};".format(beamline.name))

    def track(self, particles, beamline, **kwargs):
        """Add a ptc `track` command."""

        if kwargs.get("start"):
            self.raw("SEQEDIT, SEQUENCE={};".format(beamline.name))
            self.raw("CYCLE, START={};".format(kwargs.get("start")))
            self.raw("ENDEDIT;")
            self.raw("USE, SEQUENCE={};".format(beamline.name))

        if kwargs.get('misalignment', False):
            self.misalign(beamline)
        if kwargs.get('ptc', True):
            self.__ptc_track(particles, beamline, **kwargs)
        else:
            self.__madx_track(particles, beamline, **kwargs)

    def __madx_track(self, particles, beamline, **kwargs):
        if len(particles) == 0:
            print("No particles to track... Doing nothing.")
            return
        self.makethin(beamline.name, **kwargs)
        self._add_input('track_beamline')
        self._add_particles_for_tracking(particles)
        beamline.line.apply(lambda e: self.__generate_observation_points(e, beamline.length), axis=1)
        self._add_input('run_track_beamline')
        self._add_input('end_track')
        return self

    def __ptc_track(self, particles, beamline, **kwargs):
        if len(particles) == 0:
            print("No particles to track... Doing nothing.")
            return

        self._add_input('ptc_create_universe')
        self._add_input('ptc_create_layout', False, 2, 6, 10, True, kwargs.get('fringe', False))
        if kwargs.get('misalignment', False):
            self._add_input('ptc_misalign')
        self.__add_particles_for_tracking(particles, True)
        beamline.line.apply(lambda e: self.__generate_observation_points_ptc(e, beamline.length), axis=1)
        self._add_input('ptc_track',
                        5,
                        0.0,
                        False,
                        True,
                        1,
                        True,
                        True,
                        'ptctrack',
                        '.tfs'
                        )
        self._add_input('ptc_track_end')
        self._add_input('ptc_end')

    def match(self, **kwargs):
        seq = kwargs.get("sequence", None)
        vary = kwargs.get("vary", None)
        constraints = kwargs.get("constraints", None)
        if seq is None:
            raise MadxException("A sequence name must be provided.")
        if vary is None or len(vary) < 1:
            raise MadxException("A list of length > 0 of parameters must be provided.")
        if constraints is None:
            raise MadxException("A dictionary of constraints should be provided.")
        self._add_input('match', sequence)

    def __add_misalignment_element(self,beamline):
        """MAD-X misalignement of elements."""
        beamline.line.query("TYPE != 'MARKER'").apply(
            lambda r: self._add_input('mad_misalign_setup', r['TYPE'],
                                      np.nan_to_num(r.get('DELTAX', 0)),
                                      np.nan_to_num(r.get('DELTAY', 0)),
                                      np.nan_to_num(r.get('DELTAS', 0)),
                                      np.nan_to_num(r.get('DELTAPHI', 0)),
                                      np.nan_to_num(r.get('DELTATHETA', 0)))
            , axis=1)
