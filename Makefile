CXX ?= g++
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra
PKG_CFLAGS := $(shell pkg-config --cflags gtk+-3.0 webkit2gtk-4.1)
PKG_LIBS := $(shell pkg-config --libs gtk+-3.0 webkit2gtk-4.1)

all: raven-ui

raven-ui: main.cpp
	$(CXX) $(CXXFLAGS) $(PKG_CFLAGS) main.cpp -o raven-ui $(PKG_LIBS)

clean:
	rm -f raven-ui

.PHONY: all clean
