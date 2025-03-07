#!/usr/bin/python
#
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# coding=utf-8
"""Compiler of predicates from Logica program to SQL."""

import collections
import copy
import re
import sys
import traceback
# Uncomment to debug.
# import pprint

import json

if '.' not in __package__:
  from common import color
  from compiler import dialects
  from compiler import expr_translate
  from compiler import functors
  from compiler import rule_translate
  from parser_py import parse
  from type_inference.research import infer
else:
  from ..common import color
  from ..compiler import dialects
  from ..compiler import expr_translate
  from ..compiler import functors
  from ..compiler import rule_translate
  from ..parser_py import parse
  from ..type_inference.research import infer

PredicateInfo = collections.namedtuple('PredicateInfo',
                                       ['embeddable'])
Ground = collections.namedtuple('Ground',
                                ['table_name', 'overwrite',
                                 'copy_to_file'])

xrange = range

def FormatSql(s): return s + ';'


class Logica(object):
  """Predicate execution accumulated data.

  * Data about DEFINE TABLE and EXPORT DATA accumulated over compilation.
  * Annotations of the universe.
  """

  def __init__(self):
    # TODO: Add a comment for each member.
    self.defines = []
    self.export_statements = []
    self.defines_and_exports = []
    self.table_to_defined_table_map = {}
    # Maps a @With'ed table to SQL implementing it.
    self.table_to_with_sql_map = {}
    # Maps a table T to a list of @With'ed tables for T's query.
    self.table_to_with_dependencies = collections.defaultdict(list)
    # Maps a grounded table T to a list of With'ed tables which were compiled
    # for T. With tables need to be re-compiled for each grounded tables so
    # that Ground dependencies of the With'ed tables are added.
    self.with_compilation_done_for_parent = collections.defaultdict(set)
    self.dependency_edges = []
    self.data_dependency_edges = []
    self.table_to_export_map = {}
    self.main_predicate_sql = None
    self.preamble = ''
    # Auxiliary structure for building dependency graph. At each moment of
    # execution this is an inverse path from the final predicate to the exported
    # predicate that is being built at the moment.
    self.workflow_predicates_stack = []
    self.flags_comment = ''
    self.compiling_udf = False
    self.annotations = None
    self.custom_udfs = None
    self.custom_udf_definitions = None
    self.main_predicate = None
    self.used_predicates = []
    self.dependencies_of = None
    self.iterations = None

  def AddDefine(self, define):
    self.defines.append(define)

  def PredicateSpecificPreamble(self, predicate_name):
    needed_udfs = list(sorted([
        self.custom_udf_definitions[f]
        for f in self.dependencies_of[predicate_name]
        if f in self.custom_udf_definitions]))
    needed_semigroups = []
    for f in self.dependencies_of[predicate_name]:
      if f in self.custom_aggregation_semigroup:
        semigroup = (
          self.custom_udf_definitions[self.custom_aggregation_semigroup[f]])
        needed_semigroups.append(semigroup)
        if semigroup in needed_udfs:
          needed_udfs.remove(semigroup)
    needed_udfs = needed_semigroups + needed_udfs
    return '\n'.join(needed_udfs)

  def NeededUdfDefinitions(self):
    needed_udfs = list(sorted([
      self.custom_udf_definitions[f]
      for f in self.used_predicates if f in self.custom_udf_definitions]))
    needed_semigroups = set()
    for f in self.used_predicates:
      if f in self.custom_aggregation_semigroup:
        semigroup_definition = self.custom_udf_definitions[self.custom_aggregation_semigroup[f]]
        needed_semigroups.add(semigroup_definition)
        if semigroup_definition in needed_udfs:
          needed_udfs.remove(semigroup_definition)  # Will come as a semigroup.
    needed_udfs = list(needed_semigroups) + needed_udfs
    return needed_udfs

  def FullPreamble(self):
    return '\n'.join([self.flags_comment] + [self.preamble] + self.defines)

  def With(self, predicate_name):
    if self.compiling_udf:
      return False
    return self.annotations.With(predicate_name)


def Indent2(s):
  return '\n'.join('  ' + l for l in s.split('\n'))


def AnnotationError(message, annotation_value):
  raise rule_translate.RuleCompileException(
      message, annotation_value['__rule_text'])


class Annotations(object):
  """Utility to parse and retrieve predicate annotations."""
  ANNOTATING_PREDICATES = [
      '@Limit', '@OrderBy', '@Ground', '@Flag', '@DefineFlag',
      '@NoInject', '@Make', '@CompileAsTvf', '@With', '@NoWith',
      '@CompileAsUdf', '@ResetFlagValue', '@Dataset', '@AttachDatabase',
      '@Engine', '@Recursive', '@Iteration', '@BareAggregation'
  ]

  def __init__(self, rules, user_flags):
    # Extracting DefineFlags first, so that we can use flags in @Ground
    # annotations.
    if 'logica_default_engine' in user_flags:
      self.default_engine = user_flags['logica_default_engine']
    else:
      self.default_engine = 'duckdb'

    self.annotations = self.ExtractAnnotations(
        rules, restrict_to=['@DefineFlag', '@ResetFlagValue'])
    self.user_flags = user_flags
    self.flag_values = self.BuildFlagValues()
    # [AnnotationName][PredicateName] -> List containing the annotation.
    self.annotations.update(
        **self.ExtractAnnotations(rules, flag_values=self.flag_values))
    self.CheckAnnotatedObjects(rules)

  def Preamble(self):
    """Query preamble based on annotations and flags."""
    preamble = ''
    attach_database_statements = self.AttachDatabaseStatements()
    if attach_database_statements:
      preamble += attach_database_statements + '\n\n'
    if self.Engine() == 'psql':
      preamble += (
          '-- Initializing PostgreSQL environment.\n'
          'set client_min_messages to warning;\n'
          'create schema if not exists logica_home;\n'
          '-- Empty logica type: logicarecord893574736;\n'
          "DO $$ BEGIN if not exists (select 'I(am) :- I(think)' from pg_type where typname = 'logicarecord893574736') then create type logicarecord893574736 as (nirvana numeric); end if; END $$;\n\n")
    elif self.Engine() == 'duckdb':
      home_attachment = (
          'create schema if not exists logica_home;\n'
          if 'logica_home' not in self.AttachedDatabases()
          else '-- logica_home attached by user.\n')
      preamble += (
          '-- Initializing DuckDB environment.\n' +
          home_attachment +
          '-- Empty record, has to have a field by DuckDB syntax.\n'
          'drop type if exists logicarecord893574736 cascade; create type logicarecord893574736 as struct(nirvana numeric);\n'
      )
      if self.annotations['@Engine'].get('duckdb', {}).get('motherduck'):
        preamble += '\n'  # Sequences are not supported in MotherDuck.
      else:
        preamble += (
          'create sequence if not exists eternal_logical_sequence;\n\n')
      if self.annotations['@Engine'].get('duckdb', {}).get('threads'):
        threads = int(self.annotations['@Engine'].get('duckdb', {}).get('threads'))
        preamble += 'set threads to %d;\n' % threads
    return preamble

  def BuildFlagValues(self):
    """Building values by overriding defaults with user flags."""
    default_values = {}
    for flag, a in self.annotations['@DefineFlag'].items():
      default_values[flag] = a.get('1', '${%s}' % flag)
    programmatic_flag_values = {}
    for flag, a in self.annotations['@ResetFlagValue'].items():
      programmatic_flag_values[flag] = a.get('1', '${%s}' % flag)
    system_flags = set(['logica_default_engine'])
    allowed_flags_set = set(default_values) | system_flags

    if not set(self.user_flags) <= allowed_flags_set:
      raise rule_translate.RuleCompileException(
          'Undefined flags used: %s' % list(
              set(self.user_flags) - allowed_flags_set),
          str(set(self.user_flags) - allowed_flags_set))
    flag_values = default_values
    flag_values.update(**programmatic_flag_values)
    flag_values.update(**self.user_flags)
    return flag_values

  def NoInject(self, predicate_name):
    return predicate_name in self.annotations['@NoInject']

  def OkInjection(self, predicate_name):
    # Current annotations make predicate non-injectible. This doesn't have
    # to be the case for an arbitrary annotation.
    if (self.OrderBy(predicate_name) or self.LimitOf(predicate_name) or
        self.Ground(predicate_name) or self.NoInject(predicate_name) or
        self.ForceWith(predicate_name)):
      return False
    return True

  def AttachedDatabases(self):
    result = {}
    for k, v in self.annotations['@AttachDatabase'].items():
      if '1' not in v:
        AnnotationError('@AttachDatabase must have a single argument.',
                        v)
      result[k] = v['1']
    if (self.Engine() == 'sqlite'
        and 'logica_test' not in result
        and '@Ground' in self.annotations and
        self.annotations['@Ground']):
      result['logica_test'] = ':memory:'
    return result

  def AttachDatabaseStatements(self):
    lines = []
    for k, v in self.AttachedDatabases().items():
      type_sqlite = ''
      if self.Engine() == 'duckdb':
        lines.append('DETACH DATABASE IF EXISTS %s;' % k)
        if v[-7:] == '.sqlite':
          type_sqlite = ' (TYPE SQLITE)'
      lines.append('ATTACH DATABASE \'%s\' AS %s%s;' % (v, k, type_sqlite))
    return '\n'.join(lines)

  def CompileAsUdf(self, predicate_name):
    result = predicate_name in self.annotations['@CompileAsUdf']
    if result and self.TvfSignature(predicate_name):
      raise rule_translate.RuleCompileException(
          'A predicate can not be UDF and TVF at the '
          'same time %s.' % predicate_name,
          'Predicate: ' + predicate_name)
    return result

  def TvfSignature(self, predicate_name):
    """Table valued function signature of a predicate."""
    if predicate_name not in self.annotations['@CompileAsTvf']:
      return None
    annotation = self.annotations['@CompileAsTvf'][predicate_name]['1']
    arguments = [x['predicate_name'] for x in annotation]
    signature = ', '.join('%s ANY TABLE' % a for a in arguments)
    return 'CREATE TEMP TABLE FUNCTION %s(%s) AS ' % (predicate_name,
                                                      signature)

  def Iterations(self):
    result = {}
    for iteration_name, args in self.annotations['@Iteration'].items():
      if 'predicates' not in args:
        raise rule_translate.RuleCompileException(
          'Iteration must specify list of predicates.',
          self.annotations['@Iteration'][iteration_name]['__rule_text']
        )
      if 'repetitions' not in args:
        raise rule_translate.RuleCompileException(
          'Iteration must specify number of repetitions.',
          self.annotations['@Iteration'][iteration_name]['__rule_text']
        )
      predicates = [p['predicate_name'] for p in args['predicates']]
      result[iteration_name] = {'predicates': predicates,
                                'repetitions': args['repetitions'],
                                'stop_signal': args.get('stop_signal')}
    return result

  def LimitOf(self, predicate_name):
    """Limit of the query corresponding to the predicate as per annotation."""
    if predicate_name not in self.annotations['@Limit']:
      return None
    annotation = FieldValuesAsList(self.annotations['@Limit'][predicate_name])
    if (len(annotation) != 1 or
        not isinstance(annotation[0], int)):
      raise rule_translate.RuleCompileException(
          'Bad limit specification for predicate %s.' % predicate_name,
          'Predicate: ' + predicate_name)
    return annotation[0]

  def OrderBy(self, predicate_name):
    if predicate_name not in self.annotations['@OrderBy']:
      return None
    return FieldValuesAsList(self.annotations['@OrderBy'][predicate_name])

  def Dataset(self):
    default_dataset = 'logica_test'
    # This change is intended for all engines in the future.
    if self.Engine() in ['psql', 'duckdb']:
      default_dataset = 'logica_home'
    if self.Engine() == 'sqlite' and 'logica_home' in self.AttachedDatabases():
      default_dataset = 'logica_home'
    return self.ExtractSingleton('@Dataset', default_dataset)

  def Engine(self):
    engine = self.ExtractSingleton('@Engine', self.default_engine)
    if engine not in dialects.DIALECTS:
      AnnotationError('Unrecognized engine: %s' % engine,
                      self.annotations['@Engine'][engine])
    return engine
 
  def EngineTypechecksByDefault(self, engine):
    return engine in ['psql', 'duckdb']

  def ShouldTypecheck(self):
    engine = self.Engine()
    typechecks_by_default = self.EngineTypechecksByDefault(engine)

    if '@Engine' not in self.annotations:
      return typechecks_by_default
    if len(self.annotations['@Engine'].values()) == 0:
      return typechecks_by_default
    
    engine_annotation = list(self.annotations['@Engine'].values())[0]
    if 'type_checking' not in engine_annotation:
      return typechecks_by_default
    return engine_annotation['type_checking']

  def ExtractSingleton(self, annotation_name, default_value):
    if not self.annotations[annotation_name]:
      return default_value
    results = list(self.annotations[annotation_name].keys())
    if len(results) > 1:
      raise rule_translate.RuleCompileException(
          'Single %s must be provided. Provided: %s' % (
              annotation_name, results),
          self.annotations[annotation_name][results[0]]['__rule_text'])
    return results[0]

  def Ground(self, predicate_name):
    """Returns Ground (physical file) associated with the predicate."""
    if predicate_name not in self.annotations['@Ground']:
      return None
    annotation = self.annotations['@Ground'][predicate_name]
    table_name = annotation.get('1', self.Dataset() + '.' + predicate_name)
    if 'predicate_name' in table_name:
      other_ground = self.Ground(table_name['predicate_name'])
      if other_ground:
        table_name = other_ground.table_name
      else:
        raise rule_translate.RuleCompileException(
          'Predicate grounded to a non-grounded predicate.',
          self.annotations['@Ground'][predicate_name]['__rule_text'])
    overwrite = annotation.get('overwrite', True)
    copy_to_file = annotation.get('copy_to_file', None)
    if copy_to_file and self.Engine() != 'duckdb':
      raise rule_translate.RuleCompileException(
        'Copying to file is only supported on DuckDB engine.',
        self.annotations['@Ground'][predicate_name]['__rule_text'])
    return Ground(table_name=table_name, overwrite=overwrite,
                  copy_to_file=copy_to_file)

  def ForceWith(self, predicate_name):
    """Return true if the predicate has been explicitly marked @With."""
    return predicate_name in self.annotations['@With']

  def ForceNoWith(self, predicate_name):
    """Return true if the predicate has been explicitly marked @NoWith."""
    return predicate_name in self.annotations['@NoWith']

  def With(self, predicate_name):
    """Return whether this predicate should be compiled to a WITH-table.

    This only applies if the predicate is not inlined earlier in the flow.
    """
    is_with = self.ForceWith(predicate_name)
    is_nowith = self.ForceNoWith(predicate_name)
    if is_with and is_nowith:
      raise rule_translate.RuleCompileException(
          color.Format('Predicate is annotated both with @With and @NoWith.'),
          'Predicate: %s' % predicate_name)
    if is_with:
      return True
    if is_nowith or self.Ground(predicate_name):
      return False
    # TODO: return false for predicates that will be injected.
    return True

  def LimitClause(self, predicate_name):
    limit = self.LimitOf(predicate_name)
    if limit:
      return ' LIMIT %d' % limit
    else:
      return ''

  def OrderByClause(self, predicate_name):
    """Returns ORDER BY clause."""
    order_by = self.OrderBy(predicate_name)
    result = []
    if order_by:
      for i in range(len(order_by) - 1):
        if order_by[i+1] != 'DESC':
          result.append(order_by[i] + ',')
        else:
          result.append(order_by[i])
      result.append(order_by[-1])
      return ' ORDER BY ' + ' '.join(result)
    else:
      return ''

  def CheckAnnotatedObjects(self, rules):
    """Verify that annotations are applied to existing predicates."""
    all_predicates = {rule['head']['predicate_name'] for rule in rules} | set(
        self.annotations['@Ground']) | set(self.annotations['@Make'])
    for annotation_name in self.annotations:
      if annotation_name in {'@Limit', '@OrderBy',
                             '@NoInject', '@CompileAsTvf', '@With', '@NoWith',
                             '@CompileAsUdf'}:
        for annotated_predicate in self.annotations[annotation_name]:
          if annotated_predicate not in all_predicates:
            rule_text = self.annotations[annotation_name][annotated_predicate][
                '__rule_text']
            RaiseCompilerError(
                'Annotation %s must be applied to an existing predicate, but '
                'it was applied to a non-existing predicate %s.' %
                (color.Warn(annotation_name), color.Warn(annotated_predicate)),
                rule_text)

  @classmethod
  def ExtractAnnotations(cls, rules, restrict_to=None, flag_values=None):
    """Extracting annotations from the rules."""
    result = {p: collections.OrderedDict() for p in cls.ANNOTATING_PREDICATES}
    for rule in rules:
      rule_predicate = rule['head']['predicate_name']
      if restrict_to and rule_predicate not in restrict_to:
        continue
      if (rule_predicate[0] == '@' and
          rule_predicate not in cls.ANNOTATING_PREDICATES):
        raise rule_translate.RuleCompileException(
            'Only {0} and {1} special predicates are allowed.'.format(
                ', '.join(cls.ANNOTATING_PREDICATES[:-1]),
                cls.ANNOTATING_PREDICATES[-1]),
            rule['full_text'])
      if rule_predicate in cls.ANNOTATING_PREDICATES:
        rule_text = rule['full_text']
        # pylint: disable=cell-var-from-loop
        def ThrowException(*args, **xargs):
          _ = args
          _ = xargs
          if rule_predicate == '@Make':
            # pylint: disable=raising-format-tuple
            raise rule_translate.RuleCompileException(
                'Incorrect syntax for functor call. '
                'Functor call to be made as\n'
                '  R := F(A: V, ...)\n'
                'or\n'
                '  @Make(R, F, {A: V, ...})\n'
                'Where R, F, A\'s and V\'s are all '
                'predicate names.',
                rule_text)
          else:
            raise rule_translate.RuleCompileException(
                'Can not understand annotation.',
                rule_text)

        class Thrower(object):

          def __contains__(self, key):
            if rule_predicate == '@Make':
              ThrowException()
              return
            raise rule_translate.RuleCompileException(
                'Annotation may not use variables, but this one uses '
                'variable %s.' % (
                    color.Warn(key)),
                rule_text)
        flag_values = flag_values or Thrower()
        ql = expr_translate.QL(Thrower(), ThrowException, ThrowException,
                               flag_values)
        ql.convert_to_json = True

        annotation = rule['head']['predicate_name']
        # Checking for aggregations.
        aggregated_fields = [
          fv['field']
          for fv in rule['head']['record']['field_value']
          if 'aggregation' in fv['value']]
        if aggregated_fields:
          raise rule_translate.RuleCompileException(
                'Annotation may not use aggregation, but field '
                '%s is aggregated.' % (
                    color.Warn(aggregated_fields[0])),
                rule_text)
        field_values_json_str = ql.ConvertToSql(
            {'record': rule['head']['record']})
        try:
          field_values = json.loads(field_values_json_str)
        except:
          raise rule_translate.RuleCompileException(
              'Could not understand arguments of annotation.',
              rule['full_text'])
        if ('0' in field_values and
            isinstance(field_values['0'], dict) and
            'predicate_name' in field_values['0']):
          subject = field_values['0']['predicate_name']
        else:
          subject = field_values['0']
        del field_values['0']
        if rule_predicate in ['@OrderBy', '@Limit', '@NoInject']:
          field_values_list = FieldValuesAsList(field_values)
          if field_values_list is None:
            raise rule_translate.RuleCompileException(
                '@OrderBy and @Limit may only have positional '
                'arguments.', rule['full_text'])
          if rule_predicate == '@Limit' and len(field_values_list) != 1:
            raise rule_translate.RuleCompileException(
                'Annotation @Limit must have exactly two arguments: '
                'predicate and limit.', rule['full_text'])
        updated_annotation = result.get(annotation, {})
        field_values['__rule_text'] = rule['full_text']
        if subject in updated_annotation:
          raise rule_translate.RuleCompileException(
              color.Format(
                  '{annotation} annotates {warning}{subject}{end} more '
                  'than once: {before}, {after}',
                  dict(annotation=annotation,
                       subject=subject,
                       before=updated_annotation[subject]['__rule_text'],
                       after=field_values['__rule_text'])), rule['full_text'])
        updated_annotation[subject] = field_values
        result[annotation] = updated_annotation
    return result


class LogicaProgram(object):
  """Representing a Logica program.

  Can produce SQL for predicates.
  """

  def __init__(self, rules, table_aliases=None, user_flags=None):
    """Initializes the program.

    Args:
      rules: A list of dictionary representations of parsed Logica rules.
      table_aliases: A map from an undefined Logica predicate name to a
        BigQuery table name. This table will be used in place of predicate.
      user_flags: Dictionary of user specified flags.
    """
    rules = self.UnfoldRecursion(rules)

    # TODO: Should allocator be a member of Logica?
    self.preparsed_rules = rules
    self.rules = []
    self.defined_predicates = set()
    self.dollar_params = list(self.ExtractDollarParams(rules))
    self.table_aliases = table_aliases or {}
    self.execution = None
    self.user_flags = user_flags or {}
    self.annotations = Annotations(rules, self.user_flags)
    self.flag_values = self.annotations.flag_values
    # Dictionary custom_udfs maps function name to a format string to use
    # in queries.
    self.custom_udfs = collections.OrderedDict()
    self.custom_udf_psql_type = {}
    self.custom_aggregation_semigroup = {}
    # Dictionary custom_udf_definitions maps function name to SQL defining the
    # function.
    self.custom_udf_definitions = collections.OrderedDict()
    if not set(self.dollar_params) <= set(self.flag_values):
      raise rule_translate.RuleCompileException(
          'Parameters %s are undefined.' % (
              list(set(self.dollar_params) - set(self.flag_values))),
          str(list(set(self.dollar_params) - set(self.flag_values))))
    self.functors = None

    # Extending rules with functors.
    extended_rules = self.RunMakes(rules)  # Populates self.functors.

    # Extending rules with the library of the dialect.
    library_rules = parse.ParseFile(
        dialects.Get(self.annotations.Engine()).LibraryProgram())['rule']
    extended_rules.extend(library_rules)

    for rule in extended_rules:
      predicate_name = rule['head']['predicate_name']
      self.defined_predicates.add(predicate_name)
      self.rules.append((predicate_name, rule))
    self.CheckDistinctConsistency()
    # We need to recompute annotations, because 'Make' created more rules and
    # annotations.
    self.annotations = Annotations(extended_rules, self.user_flags)

    # Infering types if requested.
    self.typing_preamble = ''
    self.required_type_definitions = {}
    self.predicate_signatures = {}
    self.typing_engine = None
    if self.annotations.ShouldTypecheck():
      self.typing_preamble = self.RunTypechecker()

    # Build udfs, populating custom_udfs and custom_udf_definitions.
    self.BuildUdfs()
    # Function compilation may have added irrelevant defines:
    self.execution = None

  def CheckDistinctConsistency(self):
    is_distinct = {}
    for p, r in self.rules:
      distinct_before = is_distinct.get(p, None)
      distinct_here = 'distinct_denoted' in r
      if distinct_before is None:
        is_distinct[p] = distinct_here
      else:
        if distinct_before != distinct_here:
          raise rule_translate.RuleCompileException(
              color.Format(
                  'Either all rules of a predicate must be distinct denoted '
                  'or none. Predicate {warning}{p}{end} violates it.',
                  dict(p=p)), r['full_text'])

  def InscribeOrbits(self, rules, depth_map):
    """Writes satellites from annotation to a field in the body."""
    # Satellites are written to master and master is written to
    # satellites. You are responsible for those whom you tamed.
    master = {}
    for p, args in depth_map.items():
      satellite_names = []
      for s in args.get('satellites', []):
        master[s['predicate_name']] = p
        satellite_names.append(s['predicate_name'])
      if stop_predicate := args.get('stop', None):
        if stop_predicate['predicate_name'] not in satellite_names:
          if 'satellites' not in args:
            args['satellites'] = []
          args['satellites'] += [stop_predicate]
    for r in rules:
      p = r['head']['predicate_name']
      if p in depth_map:
        if 'satellites' not in depth_map[p]:
          continue
        if 'body' not in r:
          r['body'] = {'conjunction': {'conjunct': []}}
        r['body']['conjunction']['satellites'] = depth_map[p]['satellites']
      if p in master:
        if 'body' not in r:
          r['body'] = {'conjunction': {'conjunct': []}}
        r['body']['conjunction']['satellites'] = [{'predicate_name': master[p]}]

  def AddAutoStop(self, depth_map):
    for k in depth_map:
      if ('1' in depth_map[k] and
          depth_map[k]['1'] == -1 and
          'stop' not in depth_map[k]):
        depth_map[k]['stop'] = {'predicate_name': 'Stop' + k}

  def UnfoldRecursion(self, rules):
    annotations = Annotations(rules, {})
    depth_map = annotations.annotations.get('@Recursive', {})
    self.AddAutoStop(depth_map)
    self.InscribeOrbits(rules, depth_map)
    f = functors.Functors(rules)
    # Annotations are not ready at this point.
    # if (self.execution.annotations.Engine() == 'duckdb'):
    #   for p in depth_map:
    #     # DuckDB struggles with long querries.
    #     depth_map[p]['iterative'] = True
    quacks_like_a_duck = (annotations.Engine() == "duckdb")
    default_iterative = False
    default_depth = 8
    if quacks_like_a_duck:
      default_iterative = True
      default_depth = 32
    return f.UnfoldRecursions(depth_map, default_iterative, default_depth)

  def BuildUdfs(self):
    """Build UDF definitions."""
    self.InitializeExecution('@FunctionsCheck')
    self.execution.compiling_udf = True
    remove_udfs = False
    for f in self.annotations.annotations['@CompileAsUdf']:
      if not remove_udfs:
        self.custom_udfs[f] = 'DUMMY()'  # For dummy function definitions.
    # First time we create proper function definitions, when we use them.
    # TODO: If we extract signature building then we don't have to
    # compile twice.
    for _ in range(2):
      for f in self.annotations.annotations['@CompileAsUdf']:
        application, sql = self.FunctionSql(f, internal_mode=True)
        if not remove_udfs:
          self.custom_udfs[f] = application
          self.custom_udf_definitions[f] = sql
    for f in self.annotations.annotations['@BareAggregation']:
      if not remove_udfs:  # Not sure what this is.
        d = self.annotations.annotations['@BareAggregation'][f]
        if 'semigroup' not in d:
          raise rule_translate.RuleCompileException(
              color.Format(
                  'Semigroup not specified for '
                  'aggregation {warning}{f}{end}.',
                  dict(f=f)), self.annotations.annotations['@BareAggregation'][f]['__rule_text'])
        semigroup = d['semigroup']['predicate_name']
        self.custom_udfs[f] = f + '({col0})'
        if semigroup not in self.custom_udf_psql_type:
          raise rule_translate.RuleCompileException(
              color.Format(
                  'Semigroup not defined as a UDF for '
                  'aggregation {warning}{f}{end}.',
                  dict(f=f)), self.annotations.annotations['@BareAggregation'][f]['__rule_text'])
        self.custom_udf_definitions[f] = (
          'CREATE AGGREGATE %s (%s) ( '
          '  sfunc = %s, '
          '  stype = %s);' % (f,
                              self.custom_udf_psql_type[semigroup],
                              semigroup,
                              self.custom_udf_psql_type[semigroup]))
        self.custom_aggregation_semigroup[f] = semigroup

  def NewNamesAllocator(self):
    return rule_translate.NamesAllocator(custom_udfs=self.custom_udfs)

  def RunTypechecker(self):
    """Checks the program for type-correctness.

    Raises:
      TypeInferenceError if there are any type errors.
    """
    rules = [r for _, r in self.rules]
    typing_engine = infer.TypesInferenceEngine(
        rules, dialect=self.annotations.Engine())
    typing_engine.InferTypes()
    self.typing_engine = typing_engine
    type_error_checker = infer.TypeErrorChecker(rules)
    type_error_checker.CheckForError(mode='raise')
    self.predicate_signatures = typing_engine.predicate_signature
    self.required_type_definitions.update(typing_engine.collector.definitions)
    return typing_engine.typing_preamble

  def RunMakes(self, rules):
    """Runs @Make instructions."""
    if '@Make' not in self.annotations.annotations:
      return rules
    self.functors = functors.Functors(rules)
    self.functors.MakeAll(list(self.annotations.annotations['@Make'].items()))
    return self.functors.extended_rules

  @classmethod
  def ExtractDollarParamsFromString(cls, s):
    params = re.findall(r'[$][{](.*?)[}]', s)
    return set(p for p in params  # Built-in, we shouldn't override them.
               if not (p.startswith('YYYY') or p == 'MM' or p == 'DD'))

  def ExtractDollarParams(self, r):
    """Returns a set of dollar params, like ${date}."""
    # TODO: Refactor the tree scanning into a helper class.
    if isinstance(r, dict):
      member_index = sorted(r.keys())
    elif isinstance(r, list):
      member_index = range(len(r))
    else:
      if isinstance(r, str):
        return self.ExtractDollarParamsFromString(r)
      return set()

    result = set()
    for k in member_index:
      result |= self.ExtractDollarParams(r[k])
    return result

  def __str__(self):
    return str(self.preparsed_rules)

  def GetPredicateRules(self, predicate_name):
    for (n, r) in self.rules:
      if n == predicate_name:
        yield r

  def CheckOrderByClause(self, name):
    if name not in self.predicate_signatures:
      return
    if not self.annotations.OrderBy(name):
      return
    order_by_columns = set()
    for c in self.annotations.OrderBy(name):
      if c in ['desc', 'asc']:
        continue
      parts = c.split(' ')
      col = parts[0]
      order_by_columns.add(col)
    actual_columns = set(infer.ArgumentNames(self.predicate_signatures[name]))
    if '*' in actual_columns:
      # TODO: Analyze this properly. For now no evidence of error observed.
      return
    lacking_columns = ', '.join(set(order_by_columns) - set(actual_columns))
    if lacking_columns:
      raise rule_translate.RuleCompileException(
          color.Format(
              'Predicate {warning}{name}{end} is ordered by '
              'columns {warning}{columns}{end} '
              'which it lacks.', dict(name=name, columns=lacking_columns)),
              self.annotations.annotations['@OrderBy'][name]['__rule_text'])

  def PredicateSql(self, name, allocator=None, external_vocabulary=None):
    """Producing SQL for a predicate."""
    # Load proto if necessary.
    self.CheckOrderByClause(name)
    rules = list(self.GetPredicateRules(name))
    if len(rules) == 1:
      [rule] = rules
      result = (
          self.SingleRuleSql(rule, allocator, external_vocabulary,
                             must_not_be_nil=True) +
          self.annotations.OrderByClause(name) +
          self.annotations.LimitClause(name))
      # Exception should be raised by SingleRuleSql.
      assert not result.startswith('/* nil */')
      return result
    elif len(rules) > 1:
      rules_sql = []
      for rule in rules:
        if 'distinct_denoted' in rule:
          raise rule_translate.RuleCompileException(
              color.Format(
                  'For distinct denoted predicates multiple rules are not '
                  'currently supported. Consider taking '
                  '{warning}union of bodies manually{end}, if that was what '
                  'you intended.'), rule['full_text'])
        single_rule_sql = self.SingleRuleSql(
            rule, allocator, external_vocabulary)
        if not single_rule_sql.startswith('/* nil */'):
          rules_sql.append('\n%s\n' %
                          Indent2(single_rule_sql))
      if not rules_sql:
        raise rule_translate.RuleCompileException(
          'All disjuncts are nil for predicate %s.' % color.Warn(name),
          rule['full_text'])

      rules_sql = ['\n'.join('  ' + l for l in r.split('\n'))
                   for r in rules_sql]
      return 'SELECT * FROM (\n%s\n) AS UNUSED_TABLE_NAME %s %s' % (
          ' UNION ALL\n'.join(rules_sql),
          self.annotations.OrderByClause(name),
          self.annotations.LimitClause(name))
    else:
      raise rule_translate.RuleCompileException(
          color.Format(
              'No rules are defining {warning}{name}{end}, but compilation '
              'was requested.', dict(name=name)),
          r'        ¯\_(ツ)_/¯')

  @classmethod
  def TurnPositionalIntoNamed(self, select):
    """Auxiliary method transforming RuleStructure select dict.

    Args:
      select: RuleStructure select dictionary, i.e. a map from variable name
        to Logica expression of a value dictionary.

    Returns:
      newly generated select dict, where positional arguments where replaced
      with the names of variables that were standing there.
    """
    new_select = collections.OrderedDict()
    # Make UDF use positional arguments as well.
    for v in select:
      if isinstance(v, int):
        # Check for error.
        new_select[select[v]['variable']['var_name']] = select[v]
      else:
        new_select[v] = select[v]
    return new_select

  def FunctionSql(self, name, allocator=None, internal_mode=False):
    """Print formatted SQL function creation statement."""
    # TODO: Refactor this into FunctionSqlInternal and FunctionSql.
    if not allocator:
      allocator = self.NewNamesAllocator()

    rules = list(self.GetPredicateRules(name))

    # Check that the predicate is defined via a single rule.
    if not rules:
      raise rule_translate.RuleCompileException(
          color.Format(
              'No rules are defining {warning}{name}{end}, but compilation '
              'was requested.', dict(name=name)),
          r'        ¯\_(ツ)_/¯')
    elif len(rules) > 1:
      raise rule_translate.RuleCompileException(
          color.Format(
              'Predicate {warning}{name}{end} is defined by more than 1 rule '
              'and can not be compiled into a function.', dict(name=name)),
          '\n\n'.join(r['full_text'] for r in rules))
    [rule] = rules

    # Extract structure and assert that it is isomorphic to a function.
    s = rule_translate.ExtractRuleStructure(rule,
                                            external_vocabulary=None,
                                            names_allocator=allocator)

    udf_variables = [v if isinstance(v, str) else 'col%d' % v
                     for v in s.select if v != 'logica_value']
    s.select = self.TurnPositionalIntoNamed(s.select)

    variables = [v for v in s.select if v != 'logica_value']
    if 0 in variables:
      raise rule_translate.RuleCompileException(
          color.Format(
              'Predicate {warning}{name}{end} must have all aruments named for '
              'compilation as a function.',
              dict(name=name)
          ), rule['full_text'])
    for v in variables:
      if ('variable' not in s.select[v] or
          s.select[v]['variable']['var_name'] != v):
        raise rule_translate.RuleCompileException(
            color.Format(
                'Predicate {warning}{name}{end} must not rename arguments '
                'for compilation as a function.',
                dict(name=name)
            ), rule['full_text'])

    vocabulary = {v: v for v in variables}
    s.external_vocabulary = vocabulary
    self.RunInjections(s, allocator)
    s.ElliminateInternalVariables(assert_full_ellimination=True)
    s.UnificationsToConstraints()
    sql = s.AsSql(subquery_encoder=self.MakeSubqueryTranslator(allocator))
    if s.constraints or s.unnestings or s.tables:
      raise rule_translate.RuleCompileException(
          color.Format(
              'Predicate {warning}{name}{end} is not a simple function, but '
              'compilation as function was requested. Full SQL:\n{sql}',
              dict(name=name, sql=sql)
          ), rule['full_text'])
    if 'logica_value' not in s.select:
      raise rule_translate.RuleCompileException(
          color.Format(
              'Predicate {warning}{name}{end} does not have a value, but '
              'compilation as function was requested. Full SQL:\n%s' %
              sql
          ), rule['full_text'])

    # pylint: disable=g-long-lambda
    # Compile the function!
    ql = expr_translate.QL(vocabulary,
                           self.MakeSubqueryTranslator(allocator),
                           lambda message:
                           rule_translate.RuleCompileException(
                               message, rule['full_text']),
                           self.flag_values,
                           custom_udfs=self.custom_udfs,
                           dialect=self.execution.dialect)
    value_sql = ql.ConvertToSql(s.select['logica_value'])

    # TODO: Move this to dialects.py.
    if self.execution.annotations.Engine() == 'psql':
      vartype = lambda varname: (
        self.typing_engine.collector.psql_type_cache[
          s.select[varname]['type']['rendered_type']])
      sql = ('DROP FUNCTION IF EXISTS {name} CASCADE; '
             'CREATE OR REPLACE FUNCTION {name}({signature}) '
             'RETURNS {value_type} AS $$ select ({value}) '
             '$$ language sql'.format(
              name=name,
              signature=', '.join('%s %s' % (v, vartype(v))
                                  for v in variables),
              value_type=vartype('logica_value'),
              value=value_sql))
      self.custom_udf_psql_type[name] = vartype('logica_value')
    else:
      sql = 'CREATE TEMP FUNCTION {name}({signature}) AS ({value})'.format(
        name=name,
        signature=', '.join('%s ANY TYPE' % v for v in variables),
        value=value_sql)

    sql = FormatSql(sql)

    if internal_mode:
      return ('%s(%s)' % (name, ', '.join('{%s}' % v for v in udf_variables)),
              sql)

    return sql

  def InitializeExecution(self, main_predicate):
    """Initialize self.execution."""
    self.execution = Logica()
    self.execution.workflow_predicates_stack.append(main_predicate)
    self.execution.preamble = self.annotations.Preamble()
    self.execution.annotations = self.annotations
    self.execution.custom_udfs = self.custom_udfs
    self.execution.custom_udf_definitions = self.custom_udf_definitions
    self.execution.custom_aggregation_semigroup = self.custom_aggregation_semigroup
    self.execution.main_predicate = main_predicate
    self.execution.used_predicates = self.functors.args_of.get(main_predicate,
                                                               [])
    self.execution.dependencies_of = self.functors.args_of
    self.execution.dialect = dialects.Get(self.annotations.Engine())
    self.execution.iterations = self.annotations.Iterations()
  
  def UpdateExecutionWithTyping(self):
    if self.execution.dialect.IsPostgreSQLish():
      self.execution.preamble += '\n' + self.typing_preamble

  def PerformIterationClosure(self, allocator):
    """Iteration closure of current execution.
    
    If one predicates of the iteration runs, then all of them must run.
    """
    participating_predicates = list(
      self.execution.table_to_defined_table_map.keys())
    translator = self.MakeSubqueryTranslator(allocator)
    for iteration in self.execution.iterations.values():
      for p in participating_predicates:
        iteration_predicates = set(iteration['predicates'])
        if p in iteration_predicates:
          for d in iteration_predicates:
            # We are not doing anything with the resulting SQL,
            # as we only need the execution state updated.
            # We don't have actual dependency on top of stack, so we
            # do not add any dependency edge.
            translator.TranslateTable(d, None, edge_needed=False)

  def FormattedPredicateSql(self, name, allocator=None):
    """Printing top-level formatted SQL statement with defines and exports."""
    self.InitializeExecution(name)
    if self.flag_values and False:  # TODO: Control flag printing.
      flags_str_lines = ['# Logica flags:']
      for flag, value in sorted(self.flag_values.items()):
        flags_str_lines.append('#   %s = %s' % (flag, value.encode('utf-8')))
      self.execution.flags_comment = '\n'.join(flags_str_lines) + '\n\n'

    if self.annotations.CompileAsUdf(name):
      self.execution.compiling_udf = True
      sql = self.FunctionSql(name, allocator)
    else:
      sql = self.PredicateSql(name, allocator)
    self.PerformIterationClosure(allocator)

    self.UpdateExecutionWithTyping()

    assert self.execution.workflow_predicates_stack == [name], (
        'Logica internal error: unexpected workflow stack: %s' %
        self.execution.workflow_predicates_stack)

    # Wrap query in with
    with_signature = self.GenerateWithClauses(name)
    if with_signature:
      sql = '{}\n{}'.format(with_signature, sql)
    self.execution.table_to_export_map[name] = sql
    defines_and_exports = self.execution.preamble
    udf_definitions = self.execution.NeededUdfDefinitions()
    if udf_definitions:
      defines_and_exports += '\n\n'.join(udf_definitions)
      defines_and_exports += '\n\n'

    if self.execution.defines_and_exports:
      defines_and_exports += '\n\n'.join(self.execution.defines_and_exports)
      defines_and_exports += '\n\n'

    sql = self.UseFlagsAsParameters(sql)  # To avoid formatting errors.

    # Append TVF signature.
    tvf_signature = self.annotations.TvfSignature(name)
    if tvf_signature:
      sql = tvf_signature + '\n' + sql

    self.execution.main_predicate_sql = sql
    formatted_sql = (
        self.execution.flags_comment +
        defines_and_exports +
        FormatSql(sql))
    if True:
      self.execution.preamble = self.UseFlagsAsParameters(
          self.execution.preamble)
      for k, v in self.execution.table_to_export_map.items():
        self.execution.table_to_export_map[k] = self.UseFlagsAsParameters(v)
      for i, d in enumerate(self.execution.defines):
        self.execution.defines[i] = self.UseFlagsAsParameters(d)
      self.execution.flags_comment = self.UseFlagsAsParameters(
          self.execution.flags_comment)
      self.execution.main_predicate_sql = self.UseFlagsAsParameters(
        self.execution.main_predicate_sql)
      return self.UseFlagsAsParameters(formatted_sql)
    else:
      return formatted_sql

  def UseFlagsAsParameters(self, sql):
    """Running flag substitution in a loop to the fixed point."""
    # We do it in a loop to deal with flags that refer to other flags.
    prev_sql = ''
    num_subs = 0
    while sql != prev_sql:
      num_subs += 1
      prev_sql = sql
      if num_subs > 100:
        raise rule_translate.RuleCompileException(
            'You seem to have recursive flags. It is disallowed.',
            'Flags:\n' +
            '\n'.join('--{0}={1}'.format(*i)
                      for i in self.flag_values.items()))
      # Do the substitution!
      for flag, value in self.flag_values.items():
        sql = sql.replace('${%s}' % flag, value)
    return sql

  def RunInjections(self, s, allocator):
    iterations = 0
    while True:
      iterations += 1
      if iterations > sys.getrecursionlimit():
        raise rule_translate.RuleCompileException(
            RecursionError(),
            s.full_rule_text)

      new_tables = collections.OrderedDict()
      for table_name_rsql, table_predicate_rsql in s.tables.items():
        rules = list(self.GetPredicateRules(table_predicate_rsql))
        if (len(rules) == 1 and
            ('distinct_denoted' not in rules[0]) and
            self.annotations.OkInjection(table_predicate_rsql)):
          [r] = rules
          rs = rule_translate.ExtractRuleStructure(
              r, allocator, None)
          rs.ElliminateInternalVariables(assert_full_ellimination=False, unfold_records=False)
          new_tables.update(rs.tables)
          InjectStructure(s, rs)

          new_vars_map = {}
          new_inv_vars_map = {}
          for (table_name, table_var), clause_var in s.vars_map.items():
            if table_name != table_name_rsql:
              new_vars_map[table_name, table_var] = clause_var
              new_inv_vars_map[clause_var] = (table_name, table_var)
            else:
              if table_var not in rs.select:
                if '*' in rs.select:
                  subscript = {'literal': {'the_symbol': {'symbol': table_var}}}
                  s.vars_unification.append({
                      'left': {
                          'variable': {
                              'var_name': clause_var
                          }
                      },
                      'right': {
                          'subscript': {
                              'subscript': subscript,
                              'record': rs.select['*']
                          }
                      }
                  })
                elif table_var == '*':
                  s.vars_unification.append({
                    'left': {'variable': {'var_name': clause_var}},
                    'right': rs.SelectAsRecord()
                  })
                else:
                  extra_hint = '' if table_var != '*' else (
                      ' Are you using ..<rest of> for injectible predicate? '
                      'Please list the fields that you extract explicitly.')
                  raise rule_translate.RuleCompileException(
                      color.Format(
                          'Predicate {warning}{table_predicate_rsql}{end} '
                          'does not have an argument '
                          '{warning}{table_var}{end}, but '
                          'this rule tries to access it. {extra_hint}',
                          dict(table_predicate_rsql=table_predicate_rsql,
                               table_var=table_var,
                               extra_hint=extra_hint)),
                      s.full_rule_text)
              else:
                s.vars_unification.append({
                    'left': {'variable': {'var_name': clause_var}},
                    'right': rs.select[table_var]
                })
          s.vars_map = new_vars_map
          s.inv_vars_map = new_inv_vars_map
        else:
          new_tables[table_name_rsql] = table_predicate_rsql
      if s.tables == new_tables:
        break
      s.tables = new_tables

  def SingleRuleSql(self, rule,
                    allocator=None, external_vocabulary=None,
                    is_combine=False, must_not_be_nil=False):
    """Producing SQL for a given rule in the program."""
    allocator = allocator or self.NewNamesAllocator()
    r = rule
    if (is_combine):
      r = self.execution.dialect.DecorateCombineRule(r, allocator.AllocateVar())
    s = rule_translate.ExtractRuleStructure(
        r, allocator, external_vocabulary)

    # TODO(2023 July): Was this always redundant?
    # s.ElliminateInternalVariables(assert_full_ellimination=False)

    self.RunInjections(s, allocator)
    s.ElliminateInternalVariables(assert_full_ellimination=True)
    s.UnificationsToConstraints()

    if self.annotations.ShouldTypecheck():
      type_inference = infer.TypeInferenceForStructure(
          s, self.predicate_signatures, dialect=self.annotations.Engine())
      type_inference.PerformInference()
      error_checker = infer.TypeErrorChecker([type_inference.quazy_rule])
      error_checker.CheckForError('raise')
      # New types may arrive here when we have an injetible predicate with variables
      # which specific record type depends on the inputs. 
      self.required_type_definitions.update(type_inference.collector.definitions)
      self.typing_preamble = infer.BuildPreamble(self.required_type_definitions,
                                                 dialect=self.annotations.Engine())

    if 'nil' in s.tables.values():
      if must_not_be_nil:
        raise rule_translate.RuleCompileException(
          'Single rule is nil for predicate %s. '
          'Recursion unfolding failed.' % color.Warn(s.this_predicate_name),
          rule['full_text'])
      else:
        # Calling compilation could result in type error, as
        # types coming from nil are not known.
        # Return a rule marked for deletion.
        return '/* nil */ SELECT NULL FROM (SELECT 42 AS MONAD) AS NIRVANA WHERE MONAD = 0'

    try:
      sql = s.AsSql(self.MakeSubqueryTranslator(allocator), self.flag_values)
    except RuntimeError as runtime_error:
      if (str(runtime_error).startswith('maximum recursion')):
        raise rule_translate.RuleCompileException(
            RecursionError(),
            s.full_rule_text)
      else:
        raise runtime_error
    # TODO: Should this be removed?
    if 'nil' in s.tables.values():
      # Mark rule for deletion.
      sql = '/* nil */' + sql
    return sql

  def GenerateWithClauses(self, predicate_name):
    """Generate the WITH ... prefix for queries that use it."""
    dependencies = self.execution.table_to_with_dependencies[predicate_name]

    if not dependencies:
      return None

    with_bodies = []
    for dependency in dependencies:
      table_name = self.execution.table_to_defined_table_map[dependency]
      sql = self.execution.table_to_with_sql_map[table_name]
      with_bodies.append('{} AS ({})'.format(table_name, sql))

    return 'WITH {}'.format(',\n'.join(with_bodies))

  def MakeSubqueryTranslator(self, allocator):
    return SubqueryTranslator(self, allocator, self.execution)


class SubqueryTranslator(object):
  """Converter of tables and rules in a context of a universe."""

  def __init__(self, program, allocator, execution):
    self.program = program
    self.allocator = allocator
    self.execution = execution

  def TranslateTableAttachedToFile(self, table, ground, external_vocabulary,
                                   edge_needed=True):
    """Translates file-attached table. Appends exports and defines."""
    if edge_needed:
      self.execution.dependency_edges.append((
          table,
          self.execution.workflow_predicates_stack[-1]))
    if table in self.execution.table_to_defined_table_map:
      return self.execution.table_to_defined_table_map[table]
    table_name = ground.table_name
        #self.allocator.AllocateTable(hint_for_user=table)
    self.execution.table_to_defined_table_map[table] = table_name
    define_statement = '-- Interacting with table %s' % table_name
    self.execution.AddDefine(define_statement)
    export_statement = None
    if table in self.program.defined_predicates:
      self.execution.workflow_predicates_stack.append(table)
      dependency_sql = self.program.PredicateSql(
          table, self.allocator, external_vocabulary)

      # Wrap query in with
      with_signature = self.program.GenerateWithClauses(table)
      if with_signature:
        dependency_sql = '{}\n{}'.format(with_signature, dependency_sql)

      dependency_sql = self.program.UseFlagsAsParameters(dependency_sql)
      self.execution.workflow_predicates_stack.pop()
      maybe_drop_table = (
          'DROP TABLE IF EXISTS %s%s;\n' % ((
              ground.table_name if ground.overwrite else '',
              self.execution.dialect.MaybeCascadingDeletionWord())))
      maybe_copy = ''
      if ground.copy_to_file:
        maybe_copy = f'COPY {ground.table_name} TO \'{ground.copy_to_file}\';\n'
      export_statement = (
          maybe_drop_table +
          'CREATE TABLE {name} AS {dependency_sql}'.format(
              name=ground.table_name,
              dependency_sql=FormatSql(dependency_sql)) +
          maybe_copy)

      export_statement = self.program.UseFlagsAsParameters(export_statement)
      # It's cheap to store a string multiple times in Python, as it's stored
      # via a pointer.
      self.execution.table_to_export_map[table] = export_statement
      self.execution.export_statements.append(export_statement)
    if export_statement:
      self.execution.defines_and_exports.append(export_statement)
    self.execution.defines_and_exports.append(define_statement)
    return table_name

  def TranslateWithedTable(self, table):
    """Translates table that should be defined in a WITH clause."""
    parent_table = self.execution.workflow_predicates_stack[-1]
    if table not in self.execution.table_to_defined_table_map:
      table_name = self.allocator.AllocateTable(hint_for_user=table)
      self.execution.table_to_defined_table_map[table] = table_name
      # We don't pass external vocabulary; named predicates should not have
      # free terms.
      implementation = self.program.PredicateSql(table, self.allocator)
      self.execution.table_to_with_sql_map[table_name] = implementation
    else:
      # Calling predicate SQL to add the required ground dependencies.
      if table not in self.execution.with_compilation_done_for_parent[
          parent_table]:
        # Swap these lines to compile a recursive with.
        _ = self.program.PredicateSql(table, self.allocator)
        self.execution.with_compilation_done_for_parent[
            parent_table].add(table)

    # Adding dependencies at the end means we add the deepest dependencies
    # first, which ensures our WITH clause is ordered correctly.
    # Note that even if table is already defined, we need to add a dependency
    # from parent_table; table may have been defined from a different parent
    # previously.
    if table not in self.execution.table_to_with_dependencies[parent_table]:
      self.execution.table_to_with_dependencies[parent_table].append(table)
    return self.execution.table_to_defined_table_map[table]

  @classmethod
  def UnquoteParenthesised(cls, table):
    """Enable direct usage of SQL strings as table names."""
    if len(table) > 4 and table[:2] == '`(' and table[-2:] == ')`':
      return table[2:-2]
    return table

  def TranslateTable(self, table, external_vocabulary, edge_needed=True):
    """Translating table to an SQL string in the FROM cause."""
    if table in self.program.table_aliases:
      return self.program.table_aliases[table]
    ground = self.program.annotations.Ground(table)
    if ground:
      return self.TranslateTableAttachedToFile(
          table, ground, external_vocabulary, edge_needed)
    if table in self.program.defined_predicates:
      if self.program.execution.With(table):
        return self.TranslateWithedTable(table)
      return '(%s)' % self.program.PredicateSql(
          table, self.allocator, external_vocabulary)
      predicate_sql = Indent2(predicate_sql)
      return '(\n%s\n)' % predicate_sql
    self.execution.data_dependency_edges.append((
      table,
      self.execution.workflow_predicates_stack[-1]))
    return self.UnquoteParenthesised(table)

  def TranslateRule(self, rule, external_vocabulary, is_combine=False):
    return self.program.SingleRuleSql(
      rule, self.allocator, external_vocabulary,
      is_combine=is_combine)


def InjectStructure(target, source):
  """Injecting source RuleStructure into target."""
  target.vars_map.update(source.vars_map)
  target.inv_vars_map.update(source.inv_vars_map)
  target.vars_unification.extend(source.vars_unification)
  target.unnestings.extend(source.unnestings)
  target.constraints.extend(source.constraints)
  target.synonym_log.update(source.synonym_log)


def RecursionError():
  return color.Format(
      'Recursion in this rule is {warning}too deep{end}. It is running '
      'over Python defualt recursion limit. If this is intentional use '
      '{warning}sys.setrecursionlimit(10000){end} command in your '
      'notebook, or script.')


def RaiseCompilerError(message, context):
  raise rule_translate.RuleCompileException(message, context)


def FieldValuesAsList(field_values):
  field_values = copy.deepcopy(field_values)
  if '__rule_text' in field_values:
    del field_values['__rule_text']
  field_values_list = []
  for i in range(len(field_values)):
    i = str(i + 1)
    if i not in field_values:
      return None  # Error!
    field_values_list.append(field_values[i])
  return field_values_list

