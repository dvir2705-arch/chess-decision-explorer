# Chess Decision Explorer

## The problem

Chess.com shows a player their game history, but not how their decisions in
recurring positions compare to what a much larger population of games did
from the same position, or how those decisions tend to work out. A player
can see that they played a move — they can't easily see whether it was a
common choice, a rare one, or how it has scored across many games.

## Goal

Build a system that takes a player's Chess.com game history, indexes the
positions that recur within it, and compares the player's decisions and
outcomes in those positions against a large reference corpus of games. Later
stages will add Stockfish evaluation and a visual chessboard interface for
exploring the results.

## Status

Foundation only. No analysis functionality has been implemented yet.
