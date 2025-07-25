#!/usr/bin/python
#
# Copyright 2025 Google LLC
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


def Klingon(rules, predicates):
  """Renders all rules defining predicates."""
  return RenderRules(RulesOfPredicates(rules, predicates))


def RenderRule(rule):
  """Renders a rule."""
  def Modal(x):
    if rule.get('couldbe_denoted', False):
      return '{ ' + x + ' }'
    if rule.get('cantbe_denoted', False):
      return ''
    return x
  head = Modal(RenderCall(rule['head']))
  if 'body' not in rule:
    return head + '.'
  body = RenderBody(rule['body'])

  return head + ' :- ' + body + '.'


def RenderRules(rules):
  """Renders a list of rules."""
  return '\n'.join(map(RenderRule, rules))


def RulesOfPredicates(rules, predicate_names):
  """Returns rules defining predicates."""
  return [r
          for r in rules
          if r['head']['predicate_name'] in predicate_names]


def RenderCall(call):
  """Renders predicate call."""
  p = call['predicate_name']
  if p in ['=', '!=', '<', '<=', '>', '>=']:
    return RenderInfixCall(call)
  if p == 'IsNull':
    return RenderNegation(call)
  return RenderPositionalCall(call)

def RenderNegation(call):
  """Renders negation of a call."""
  p = call['predicate_name']
  record = call['record']
  fvs = record['field_value']
  predicate = call['predicate_name']
  body = fvs[0]['value']['expression']['combine']['body']
  conjuncts = body['conjunction']['conjunct']
  assert len(conjuncts) == 1
  negated_expr = conjuncts[0]['predicate']
  return 'not ' + RenderCall(negated_expr)
  

def RenderInfixCall(call):
  """Fenders infix call."""
  record = call['record']
  fvs = record['field_value']
  predicate = call['predicate_name']
  assert fvs[0]['field'] == 'left'
  assert fvs[1]['field'] == 'right'
  return (RenderExpression(fvs[0]['value']['expression']) +
          predicate +
          RenderExpression(fvs[1]['value']['expression']))


def RenderPositionalCall(call):
  """Renders call of predicate with positional arguments."""
  record = call['record']
  fvs = record['field_value']
  values = []
  for i, fv in enumerate(fvs):
    assert i == fv['field'], (
        'Bad argument:' + str(i) + ' vs ' + str(fv['field']))
    values.append(RenderExpression(fv['value']['expression']))
  return Snakify(call['predicate_name']) + '(' + ','.join(values) + ')'


def RenderBody(body):
  """Renders body of rule."""
  calls = []
  for x in body['conjunction']['conjunct']:
    c = x['predicate']
    calls.append(RenderCall(c))
  return ', '.join(calls)


def Snakify(camel):
  """Snakifies a camel."""
  result = [camel[0]]
  for char in camel[1:]:
    if char.isupper():
      result.append('_')
    result.append(char.lower())
  return ''.join(result).lower()


###############################
# Rendering Expression.

def RenderExpression(e):
  """Renders an expression."""
  if 'literal' in e:
    return RenderLiteral(e['literal'])
  if 'variable' in e:
    return RenderVariable(e['variable'])

def RenderLiteral(l):
  """Renders a literal. For now just string."""
  return l['the_string']['the_string']


def RenderVariable(v):
  """Renders a variable."""
  return v['var_name'].upper()


####################
# Running Clingo.

def RunClingo(program):
  """Running program on clingo, returning models."""
  import clingo
  ctl = clingo.Control()
  ctl.add("base", [], program)
  ctl.configuration.solve.models = 0
  if False:
    ctl.configuration.solve.opt_mode = 'opt'
  ctl.ground([("base", [])])
  result = []
  with ctl.solve(yield_=True) as handle:
    for model_id, model in enumerate(handle):
      entry = []
      for s in model.symbols(atoms=True):
        entry.append({'predicate': s.name,
                      'args': list(map(str, s.arguments))})
      result.append({'model': entry, 'model_id': model_id})
  return result

#############################
# Rendering clingo models.

def RenderKlingonCall(c, from_logica=False):
  """Renders a Klingon call."""
  def RenderArgs(args):
    return ','.join(args)
  def RenderPredicate(p):
    if from_logica:
      return Snakify(p)
    return p
  return RenderPredicate(c['predicate']) + '(' + RenderArgs(c['args']) + ').'


def RenderKlingonModel(calls, from_logica=False):
  """Renders a Klingon model."""
  return list(RenderKlingonCall(c, from_logica=from_logica) for c in calls)


def RenderKlingon(models):
  """Renders a list of models to show."""
  lines = []
  for m in models:
    lines.append('# Model: %d' % m['model_id'])
    lines.extend(RenderKlingonModel(m['model']))
  return '\n'.join(lines)