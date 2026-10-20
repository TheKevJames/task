task
====

Simple personal task manager, shared by an operator and agents.

Installation
------------

.. code-block:: console

    $ uv tool install 'task @ git+https://github.com/TheKevJames/task@8.2.1'

Usage
-----

Tasks are stored under ``$TASK_FOLDER``: every namespace in one database,
``tasks.sqlite``, and each namespace's descriptions in a directory of its own.
The first run of 9.x imports a store written by 8.x, so upgrade every device
sharing ``$TASK_FOLDER`` before using any of them.

.. code-block:: console

    $ export TASK_FOLDER=~/tasks
    $ task --help

Agents
------

Whoever runs ``task`` directly is the operator, writing the files under no
rules. An agent instead gets no write access to ``$TASK_FOLDER``, and a server
which writes for it under its rules (see ``task/rules.py`` for their format):

.. code-block:: console

    $ task serve /run/triage.sock --rules rules.toml --agent triage --as triage/1

With ``$TASK_SERVER`` set to that socket, the agent's ``task`` runs every
command on the server, as that agent. The socket's path is the agent's
identity, so it must be one only that agent can reach. A program which knows
when its agents start and stop instead embeds the server, with
``task.cli.listen``, and its own checks as an ``ops.Guard``.

An agent claims every task it creates or changes, and may change only tasks
which are unclaimed or its own; ``next`` claims the highest-priority unclaimed
match for it. The operator changes only unclaimed tasks or their own, unless
they ``--force`` it. See ``GLOSSARY.md`` and ``docs/adr/``.

Conversation
------------

Anyone may comment on a task, or ask the operator a question on it, without
claiming it; ``task`` stamps each with who said it and when. Only the operator
answers, by the question's number:

.. code-block:: console

    $ task 3 set --question 'Pin binutils, or bump the toolchain?'
    $ task list -f questions=open
    $ task 3 set --answer 'Q1: pin it' --comment 'see the build log'

Tasks with no open questions are ``-f questions!=open``.

Relations
---------

A task may wait on others, or on something outside the task list: on another
task's current occurrence being marked as done, on another task being decided
(out of triage, not blocked, and with no open questions), or on a condition at
a URL, which only someone removing it can clear. A task for the same work as
another is its duplicate, and goes once that one is done:

.. code-block:: console

    $ task 4 set --blocked-by bakery:184:decided --blocked-by 2
    $ task 5 set --blocked-by 'https://github.com/a/b/issues/4: closed'
    $ task 6 set --duplicate-of 3
    $ task list -f blocked=yes

Blocked tasks are hidden from ``list``, ``due``, and ``next`` unless asked for
(``-f blocked=yes`` or ``-f blocked=any``), and marked ``×`` when shown. Once
all a task waits on has cleared, ``task`` moves it back to triage, saying so in
a comment. ``task 5 unset --blocked-by https://github.com/a/b/issues/4`` clears
a blocker, and ``task 6 unset duplicate_of`` makes a duplicate a task of its
own again. ``task`` refuses relations to tasks which do not exist, which could
never clear, or which would make tasks wait on each other.
