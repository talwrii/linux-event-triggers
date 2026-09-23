# linux-event-triggers
Execute callbacks such as running commands when a linux event device such as a keyboard or keyboard-emulating device) receives an event. Prevent other processes from receiving the event.

AI-generated and unreviewed.

## Motivation
Macroboards and footpedals have a habit of showing up as a keyboard device with fairly standard keys like "a", "b", "c". These conflict with keys that you are already using, for example to insert the character "a", so the entire device must be handled separately. `linux-event-triggers` is a tool to do this.

## Alternatives and prior work
`kbd` can remap keys for keyboards.  It also supports `command` to run commands. I already used `kbd` as a separate device and didn't want to mix up another device and activity in this.

`triggerhappy` basically does the same thing as this program. However, when running in non-daemon mode it does not seem able to grab the event (or so claude says) and I don't really want to mess around with daemons.

You could buy a usb midi device rather than a keyboard emulating device for this purpose. midi tends to make it easier to respond to an event from a single device. The author has written a tool called `midi-exec` for this purpose, but it mostly a DSL surrounding python libraries.

## Installation

`pipx install linux-event-triggers`

## Usage
To print `hello` when `a` is pressed on a device, $DEVICE,
you can use the following.

`evtrig $DEVICE --chown --bind "a=echo hello"`

This will attempt to use sudo to chown the device file on initial run, potentially prompting you for a password.

You can use udevadm to find a *fixed* path for the event device. This will use the USB device name than a name like `event17`


Once you have this working you will likely want to remove the need for a password in sudo. This can be acheived by editting sudoers to support the specific command you need without a password, or wrangling linuxes `udev` device registering system to change ownership. I specifically provided a means of working around udev with `sudo` because udev is too hard to use.


