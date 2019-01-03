from __future__ import print_function, division, absolute_import
import operator
import functools

import hypothesis
import hypothesis.stateful
import hypothesis.strategies as st
import hypothesis.internal.conjecture.utils as cu
from hypothesis.searchstrategy.strategies import SearchStrategy
from nose.tools import assert_equal

import redis
import fakeredis


self_strategy = st.runner()


class AttrSamplingStrategy(SearchStrategy):
    """Strategy for sampling a specific field from a state machine"""

    def __init__(self, name):
        self.name = name

    def do_draw(self, data):
        machine = data.draw(self_strategy)
        values = getattr(machine, self.name)
        position = cu.integer_range(data, 0, len(values) - 1)
        return values[position]


keys = AttrSamplingStrategy('keys')
fields = AttrSamplingStrategy('fields')
values = AttrSamplingStrategy('values')
scores = AttrSamplingStrategy('scores')

int_as_bytes = st.builds(lambda x: str(x).encode(), st.integers())
float_as_bytes = st.builds(lambda x: repr(x).encode(), st.floats(width=32))
counts = st.integers(min_value=-3, max_value=3) | st.integers()
limits = st.just(()) | st.tuples(st.just('limit'), counts, counts)
# Redis has an integer overflow bug in swapdb, so we confine the numbers to
# a limited range (https://github.com/antirez/redis/issues/5737).
dbnums = st.integers(min_value=0, max_value=3) | st.integers(min_value=-1000, max_value=1000)
# The filter is to work around https://github.com/antirez/redis/issues/5632
patterns = (st.text(alphabet=st.sampled_from('[]^$*.?-azAZ\\\r\n\t'))
            | st.binary().filter(lambda x: b'\0' not in x))
score_tests = scores | st.builds(lambda x: b'(' + repr(x).encode(), scores)
string_tests = (
    st.sampled_from([b'+', b'-'])
    | st.builds(operator.add, st.sampled_from([b'(', b'[']), fields))


class WrappedException(object):
    """Wraps an exception for the purposes of comparison."""
    def __init__(self, exc):
        self.wrapped = exc

    def __str__(self):
        return str(self.wrapped)

    def __repr__(self):
        return 'WrappedException({0!r})'.format(self.wrapped)

    def __eq__(self, other):
        if not isinstance(other, WrappedException):
            return NotImplemented
        if type(self.wrapped) != type(other.wrapped):    # noqa: E721
            return False
        # TODO: re-enable after more carefully handling order of error checks
        # return self.wrapped.args == other.wrapped.args
        return True

    def __ne__(self, other):
        if not isinstance(other, WrappedException):
            return NotImplemented
        return not self == other


def wrap_exceptions(obj):
    if isinstance(obj, list):
        return [wrap_exceptions(item) for item in obj]
    elif isinstance(obj, Exception):
        return WrappedException(obj)
    else:
        return obj


def sort_list(lst):
    if isinstance(lst, list):
        return sorted(lst)
    else:
        return lst


def flatten(args):
    if isinstance(args, (list, tuple)):
        for arg in args:
            for item in flatten(arg):
                yield item
    elif args is not None:
        yield args


def default_normalize(x):
    return x


class Command(object):
    def __init__(self, *args, **kwargs):
        self.args = tuple(flatten(args))
        self.normalize = kwargs.pop('normalize', default_normalize)
        if kwargs:
            raise TypeError('Unexpected keyword args {}'.format(kwargs))

    def __repr__(self):
        parts = [repr(arg) for arg in self.args]
        if self.normalize is not default_normalize:
            parts.append('normalize={!r}'.format(self.normalize))
        return 'Command({})'.format(', '.join(parts))


def commands(*args, **kwargs):
    return st.builds(functools.partial(Command, **kwargs), *args)


# TODO: all expiry-related commands
common_commands = (
    commands(st.sampled_from(['del', 'exists', 'persist', 'type']), keys)
    | commands(st.just('keys'), st.just('*'), normalize=sort_list)
    # Disabled for now due to redis giving wrong answers
    # (https://github.com/antirez/redis/issues/5632)
    # | st.tuples(st.just('keys'), patterns)
    | commands(st.just('move'), keys, dbnums)
    | commands(st.sampled_from(['rename', 'renamenx']), keys, keys)
    # TODO: find a better solution to sort instability than throwing
    # away the sort entirely with normalize. This also prevents us
    # using LIMIT.
    | commands(st.just('sort'), keys,
               st.none() | st.just('asc'),
               st.none() | st.just('desc'),
               st.none() | st.just('alpha'),
               normalize=sort_list)
)

# TODO: tests for select
connection_commands = (
    commands(st.just('echo'), values)
    | commands(st.just('ping'), st.lists(values, max_size=2))
    | commands(st.just('swapdb'), dbnums, dbnums)
)

string_create_commands = commands(st.just('set'), keys, values)
string_commands = (
    commands(st.just('append'), keys, values)
    | commands(st.just('bitcount'), keys)
    | commands(st.just('bitcount'), keys, values, values)
    | commands(st.sampled_from(['incr', 'decr']), keys)
    | commands(st.sampled_from(['incrby', 'decrby']), keys, values)
    # Disabled for now because Python can't exactly model the long doubles.
    # TODO: make a more targeted test that checks the basics.
    # TODO: check how it gets stringified, without relying on hypothesis
    # to get generate a get call before it gets overwritten.
    # | commands(st.just('incrbyfloat'), keys, st.floats(width=32))
    | commands(st.just('get'), keys)
    | commands(st.just('getbit'), keys, counts)
    | commands(st.just('setbit'), keys, counts,
               st.integers(min_value=0, max_value=1) | st.integers())
    | commands(st.sampled_from(['substr', 'getrange']), keys, counts, counts)
    | commands(st.just('getset'), keys, values)
    | commands(st.just('mget'), st.lists(keys))
    | commands(st.sampled_from(['mset', 'msetnx']), st.lists(st.tuples(keys, values)))
    | commands(st.just('set'), keys, values,
               st.none() | st.just('nx'), st.none() | st.just('xx'))
    | commands(st.just('setex'), keys, st.integers(min_value=1000000000), values)
    | commands(st.just('psetex'), keys, st.integers(min_value=1000000000000), values)
    | commands(st.just('setnx'), keys, values)
    | commands(st.just('setrange'), keys, counts, values)
    | commands(st.just('strlen'), keys)
)

# TODO: add a test for hincrbyfloat. See incrbyfloat for why this is
# problematic.
hash_create_commands = (
    commands(st.just('hmset'), keys, st.lists(st.tuples(fields, values), min_size=1))
)
hash_commands = (
    commands(st.just('hmset'), keys, st.lists(st.tuples(fields, values)))
    | commands(st.just('hdel'), keys, st.lists(fields))
    | commands(st.just('hexists'), keys, fields)
    | commands(st.just('hget'), keys, fields)
    | commands(st.sampled_from(['hgetall', 'hkeys', 'hvals']), keys, normalize=sort_list)
    | commands(st.just('hincrby'), keys, fields, st.integers())
    | commands(st.just('hlen'), keys)
    | commands(st.just('hmget'), keys, st.lists(fields))
    | commands(st.just('hmset'), keys, st.lists(st.tuples(fields, values)))
    | commands(st.sampled_from(['hset', 'hsetnx']), keys, fields, values)
    | commands(st.just('hstrlen'), keys, fields)
)

# TODO: blocking commands
list_create_commands = commands(st.just('rpush'), keys, st.lists(values, min_size=1))
list_commands = (
    commands(st.just('lindex'), keys, counts)
    | commands(st.just('linsert'), keys,
               st.sampled_from(['before', 'after', 'BEFORE', 'AFTER']) | st.binary(),
               values, values)
    | commands(st.just('llen'), keys)
    | commands(st.sampled_from(['lpop', 'rpop']), keys)
    | commands(st.sampled_from(['lpush', 'lpushx', 'rpush', 'rpushx']), keys, st.lists(values))
    | commands(st.just('lrange'), keys, counts, counts)
    | commands(st.just('lrem'), keys, counts, values)
    | commands(st.just('lset'), keys, counts, values)
    | commands(st.just('ltrim'), keys, counts, counts)
    | commands(st.just('rpoplpush'), keys, keys)
)

# TODO:
# - find a way to test srandmember, spop which are random
# - sscan
set_create_commands = (
    commands(st.just('sadd'), keys, st.lists(fields, min_size=1))
)
set_commands = (
    commands(st.just('sadd'), keys, st.lists(fields,))
    | commands(st.just('scard'), keys)
    | commands(st.sampled_from(['sdiff', 'sinter', 'sunion']), st.lists(keys), normalize=sort_list)
    | commands(st.sampled_from(['sdiffstore', 'sinterstore', 'sunionstore']),
               keys, st.lists(keys), normalize=sort_list)
    | commands(st.just('sismember'), keys, fields)
    | commands(st.just('smembers'), keys, normalize=sort_list)
    | commands(st.just('smove'), keys, keys, fields)
    | commands(st.just('srem'), keys, st.lists(fields))
)


def build_zstore(command, dest, sources, weights, aggregate):
    args = [command, dest, len(sources)]
    args += [source[0] for source in sources]
    if weights:
        args.append('weights')
        args += [source[1] for source in sources]
    if aggregate:
        args += ['aggregate', aggregate]
    return Command(args)


# TODO: zscan, zpopmin/zpopmax, bzpopmin/bzpopmax, probably more
zset_create_commands = (
    commands(st.just('zadd'), keys, st.lists(st.tuples(scores, fields), min_size=1))
)
zset_commands = (
    # TODO: test xx, nx, ch, incr
    commands(st.just('zadd'), keys, st.lists(st.tuples(scores, fields)))
    | commands(st.just('zcard'), keys)
    | commands(st.just('zcount'), keys, score_tests, score_tests)
    | commands(st.just('zincrby'), keys, scores, fields)
    | commands(st.sampled_from(['zrange', 'zrevrange']), keys, counts, counts,
               st.none() | st.just('withscores'))
    | commands(st.sampled_from(['zrangebyscore', 'zrevrangebyscore']),
               keys, score_tests, score_tests,
               limits,
               st.none() | st.just('withscores'))
    | commands(st.sampled_from(['zrank', 'zrevrank']), keys, fields)
    | commands(st.just('zrem'), keys, st.lists(fields))
    | commands(st.just('zremrangebyrank'), keys, counts, counts)
    | commands(st.just('zremrangebyscore'), keys, score_tests, score_tests)
    | commands(st.just('zscore'), keys, fields)
    | st.builds(build_zstore,
                command=st.sampled_from(['zunionstore', 'zinterstore']),
                dest=keys, sources=st.lists(st.tuples(keys, float_as_bytes)),
                weights=st.booleans(),
                aggregate=st.sampled_from([None, 'sum', 'min', 'max']))
)

zset_no_score_create_commands = (
    commands(st.just('zadd'), keys, st.lists(st.tuples(st.just(0), fields), min_size=1))
)
zset_no_score_commands = (
    # TODO: test xx, nx, ch, incr
    commands(st.just('zadd'), keys, st.lists(st.tuples(st.just(0), fields)))
    | commands(st.just('zlexcount'), keys, string_tests, string_tests)
    | commands(st.sampled_from(['zrangebylex', 'zrevrangebylex']),
               keys, string_tests, string_tests,
               limits)
    | commands(st.just('zremrangebylex'), keys, string_tests, string_tests)
)

transaction_commands = (
    commands(st.sampled_from(['multi', 'discard', 'exec', 'unwatch']))
    | commands(st.just('watch'), keys)
)

server_commands = (
    # TODO: real redis raises an error if there is a save already in progress.
    # Find a better way to test this.
    # commands(st.just('bgsave'))
    commands(st.sampled_from(['flushdb', 'flushall']), st.sampled_from([[], 'async']))
    # TODO: result is non-deterministic
    # | commands(st.just('lastsave'))
    | commands(st.just('save'))
)

bad_commands = (
    # redis-py splits the command on spaces, and hangs if that ends up
    # being an empty list
    commands(st.text().filter(lambda x: bool(x.split())),
             st.lists(st.binary() | st.text()))
)


@hypothesis.settings(max_examples=1000, timeout=hypothesis.unlimited)
class CommonMachine(hypothesis.stateful.GenericStateMachine):
    create_command_strategy = None

    STATE_EMPTY = 0
    STATE_INIT = 1
    STATE_RUNNING = 2

    def __init__(self):
        super(CommonMachine, self).__init__()
        self.fake = fakeredis.FakeStrictRedis()
        self.real = redis.StrictRedis('localhost', port=6379)
        self.transaction_normalize = []
        self.keys = []
        self.fields = []
        self.values = []
        self.scores = []
        self.state = self.STATE_EMPTY
        try:
            self.real.execute_command('discard')
        except redis.ResponseError:
            pass
        self.real.flushall()

    def teardown(self):
        self.real.connection_pool.disconnect()
        self.fake.connection_pool.disconnect()
        super(CommonMachine, self).teardown()

    def _evaluate(self, client, command):
        try:
            result = client.execute_command(*command.args)
            if result != 'QUEUED':
                result = command.normalize(result)
            exc = None
        except Exception as e:
            result = exc = e
        return wrap_exceptions(result), exc

    def _compare(self, command):
        fake_result, fake_exc = self._evaluate(self.fake, command)
        real_result, real_exc = self._evaluate(self.real, command)

        if fake_exc is not None and real_exc is None:
            raise fake_exc
        elif real_exc is not None and fake_exc is None:
            assert_equal(real_exc, fake_exc, "Expected exception {0} not raised".format(real_exc))
        elif (real_exc is None and isinstance(real_result, list)
              and command.args and command.args[0].lower() == 'exec'):
            # Transactions need to use the normalize functions of the
            # component commands.
            assert_equal(len(self.transaction_normalize), len(real_result))
            assert_equal(len(self.transaction_normalize), len(fake_result))
            for n, r, f in zip(self.transaction_normalize, real_result, fake_result):
                assert_equal(n(f), n(r))
            self.transaction_normalize = []
        elif real_exc is None and command.args and command.args[0].lower() == 'discard':
            self.transaction_normalize = []
        else:
            assert_equal(fake_result, real_result)
            if real_result == b'QUEUED':
                # Since redis removes the distinction between simple strings and
                # bulk strings, this might not actually indicate that we're in a
                # transaction. But it is extremely unlikely that hypothesis will
                # find such examples.
                self.transaction_normalize.append(command.normalize)

    def _init_attrs(self, attrs):
        for key, value in attrs.items():
            setattr(self, key, value)

    def _init_data(self, init_commands):
        for command in init_commands:
            self._compare(command)

    def steps(self):
        if self.state == self.STATE_EMPTY:
            attrs = {
                'keys': st.lists(st.binary(), min_size=2, unique=True),
                'fields': st.lists(st.binary(), min_size=2, unique=True),
                'values': st.lists(st.binary() | int_as_bytes | float_as_bytes,
                                   min_size=2, unique=True),
                'scores': st.lists(st.floats(width=32), min_size=2, unique=True)
            }
            return st.fixed_dictionaries(attrs)
        elif self.state == self.STATE_INIT:
            return st.lists(self.create_command_strategy)
        else:
            return self.command_strategy

    def execute_step(self, step):
        if self.state == self.STATE_EMPTY:
            self._init_attrs(step)
            self.state = self.STATE_INIT if self.create_command_strategy else self.STATE_RUNNING
        elif self.state == self.STATE_INIT:
            self._init_data(step)
            self.state = self.STATE_RUNNING
        else:
            self._compare(step)


class ConnectionMachine(CommonMachine):
    command_strategy = connection_commands | common_commands


TestConnection = ConnectionMachine.TestCase


class StringMachine(CommonMachine):
    create_command_strategy = string_create_commands
    command_strategy = string_commands | common_commands


TestString = StringMachine.TestCase


class HashMachine(CommonMachine):
    create_command_strategy = hash_create_commands
    command_strategy = hash_commands | common_commands


TestHash = HashMachine.TestCase


class ListMachine(CommonMachine):
    create_command_strategy = list_create_commands
    command_strategy = list_commands | common_commands


TestList = ListMachine.TestCase


class SetMachine(CommonMachine):
    create_command_strategy = set_create_commands
    command_strategy = set_commands | common_commands


TestSet = SetMachine.TestCase


class ZSetMachine(CommonMachine):
    create_command_strategy = zset_create_commands
    command_strategy = zset_commands | common_commands


TestZSet = ZSetMachine.TestCase


class ZSetNoScoresMachine(CommonMachine):
    create_command_strategy = zset_no_score_create_commands
    command_strategy = zset_no_score_commands | common_commands


TestZSetNoScores = ZSetNoScoresMachine.TestCase


class TransactionMachine(CommonMachine):
    create_command_strategy = string_create_commands
    command_strategy = transaction_commands | string_commands | common_commands


TestTransaction = TransactionMachine.TestCase


class ServerMachine(CommonMachine):
    create_command_strategy = string_create_commands
    command_strategy = server_commands | string_commands | common_commands


TestServer = ServerMachine.TestCase


class JointMachine(CommonMachine):
    create_command_strategy = (
        string_create_commands | hash_create_commands | list_create_commands
        | set_create_commands | zset_create_commands)
    command_strategy = (
        transaction_commands | server_commands | connection_commands
        | string_commands | hash_commands | list_commands | set_commands
        | zset_commands | common_commands | bad_commands)


TestJoint = JointMachine.TestCase
