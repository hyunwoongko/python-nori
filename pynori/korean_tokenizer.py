import os
import gc
import sys
import unicodedata
from datetime import datetime
from configparser import ConfigParser

#from enum import Enum, unique, auto 
from pynori.char_unicode import *
from pynori.dict.connection_costs import ConnectionCosts
from pynori.dict.user_dictionary import UserDictionary
from pynori.dict.known_dictionary import KnownDictionary
from pynori.dict.unknown_dictionary import UnknownDictionary
from pynori.dict.character_definition import CharacterDefinition, character_category_map
from pynori.dictionary_token import DictionaryToken
from pynori.decompound_token import DecompoundToken
from pynori.pos import POS
from pynori.token_attribute import TokenAttribute


cfg = ConfigParser()
PATH_CUR = os.path.dirname(__file__)
cfg.read(PATH_CUR+'/config.ini')

PATH_KN_DICT = cfg['PATH']['KN_DICT']
PATH_UNK_DICT = cfg['PATH']['UNK_DICT']
PATH_CONN_COST = cfg['PATH']['CONN_COST']


class Type(object):
	"""Token type reflecting the original source of this token"""
	KNOWN = 'KN' # Known words from the system dictionary.
	UNKNOWN = 'UKN' # Unknown words (heuristically segmented).
	USER = 'US' # Known words from the user dictionary.

class DcpdMode(object):
	"""Decompound mode: this determines how the tokenizer handles"""
	NONE = 'NONE' # No decomposition for compound.
	DISCARD = 'DISCARD' # Decompose compounds and discards the original form (default).
	MIXED = 'MIXED' # Decompose compounds and keeps the original form.


class KoreanTokenizer(object):
	"""Tokenizer for Korean text.

	KoreanTokenizer - Split input string into several tokens.

	Parameters
	----------
	decompound_mode : {'NONE', 'DISCARD', 'MIXED'}
		this determines how the tokenizer handles common compound words, remaining the root(original) word or not.
	
	infl_decompound_mode : {'NONE', 'DISCARD', 'MIXED'}
		this determines how the tokenizer handles inflect compound words, remaining the root(original) word or not.

	output_unknown_unigrams : {'True', 'False'}
		true if outputs unigrams for unknown words.

	discard_punctuation : {'True', 'False'}
		true if punctuation tokens should be dropped from the output.

	Notes
	-----
	This tokenizer uses a rolling viterbi search to find 
	the least cost segmentation (path) of the incoming characters.

	"""

	# For safety
	MAX_UNKNOWN_WORD_LENGTH = 1024
	MAX_BACKTRACE_GAP = 1024
	
	def __init__(self, 
				 verbose,
				 path_userdict,
				 decompound_mode,
				 infl_decompound_mode,
				 output_unknown_unigrams,
				 discard_punctuation):
		self.mode = decompound_mode
		self.infl_mode = infl_decompound_mode
		self.output_unknown_unigrams = output_unknown_unigrams
		self.discard_punctuation = discard_punctuation
		self.verbose = verbose
		gc.disable()
		self.buffer = KoreanTokenizer.Buffer()
		self.character_definition = CharacterDefinition()
		self.user_dict = UserDictionary.open(PATH_CUR+path_userdict)
		#start_main = datetime.now()
		self.kn_dict = KnownDictionary.open(PATH_CUR+PATH_KN_DICT)
		#end_main = datetime.now()
		#print('\n RUN TIME: {}\n'.format(end_main - start_main))
		self.unk_dict = UnknownDictionary.open(PATH_CUR+PATH_UNK_DICT)
		self.conn_costs = ConnectionCosts.open(PATH_CUR+PATH_CONN_COST)
		gc.enable()
		self.reset_state()

	def reset_state(self):
		self.pos = 0
		self.end = False
		self.last_backtrace_pos = 0
		self.positions = KoreanTokenizer.WrappedPositionArray()
		self.tkn_attr_obj = TokenAttribute()
		# Already parsed, but not yet passed to caller, tokens
		self.pending = []
		# Add BOS
		self.positions.get(0).add(0, 0, -1, -1, -1, -1, Type.KNOWN, None, None, None)

	def set_input(self, in_string):
		"""Load input korean string to buffer."""

		# For Exception: out of character unicode range
		new_string = ""
		for ch in in_string:
			if character_category_map(ch) is None:
				new_string += ' ' # character_category_map 범위에 없는 경우 그냥 공백으로 대체
			else:
				new_string += ch
		#in_string = in_string.replace('\xa0', ' ')

		self.buffer.set(new_string)
		#self.buffer.set(in_string)
		self.reset_state()


	class Buffer(object):
		"""Bring the input korean text."""

		def set(self, in_string):
			self.in_string = in_string

		def get(self, pos):
			if pos >= 0 and pos <= len(self.in_string)-1:
				result = self.in_string[pos]
			else:
				result = -1
			return result

		def slice_get(self, start_pos, end_pos_plus1):
			return self.in_string[start_pos:end_pos_plus1]


	class Position(object):
		"""Holds all back pointers arriving to this position."""

		def __init__(self):
			self.pos = 0
			self.count = 0	# it is array length, not simple index!
			self.costs = []
			self.lastRightID = []
			self.backPos = []
			self.backWordPos = []
			self.backIndex = []
			self.backID = []
			self.backDictType = []
			# below: added for pynori
			self.backPosType = []
			self.morphemes = []
			self.backPosTag = []

		def grow(self):
			pass

		def add(self, cost, lastRightID, backPos, backRPos, backIndex, backID, backDictType, backPosType, morphemes, backPosTag):
			""" 
			NOTE: this isn't quite a true Viterbi search,
			because we should check if lastRightID is
			already present here, and only update if the new
			cost is less than the current cost, instead of
			simply appending.  However, that will likely hurt
			performance (usually we add a lastRightID only once),
			and it means we actually create the full graph
			intersection instead of a "normal" Viterbi lattice:
			"""
			if self.count == len(self.costs):
				self.grow()
			self.costs.append(cost) 
			self.lastRightID.append(lastRightID)
			self.backPos.append(backPos)
			self.backWordPos.append(backRPos)
			self.backIndex.append(backIndex)
			self.backID.append(backID) # ID 대신에 그냥 surface 그대로 넣자.
			self.backDictType.append(backDictType)
			self.count += 1
			# below: added for pynori
			self.backPosType.append(backPosType)
			self.morphemes.append(morphemes)
			self.backPosTag.append(backPosTag)
				
		def reset(self):
			self.count = 0
		

	"""	
	TODO: make generic'd version of this "circular array"?
	It's a bit tricky because we do things to the Position
	(eg, set .pos = N on reuse)...
	"""
	class WrappedPositionArray(object):
		""""""

		def __init__(self):
			self.positions = []
			for _ in range(0, 4):
				self.positions.append(KoreanTokenizer.Position())
		
			# Next array index to write to in positions:
			self.nextWrite = 0
			# Next position to write:
			self.nextPos = 0
			# How many valid Position instances are held in the positions array:
			self.count = 0

		def reset(self):
			self.nextWrite -= 1
			while self.count > 0:
				if self.nextWrite == -1:
					self.nextWrite = len(self.positions) - 1

				self.positions[self.nextWrite].reset()
				self.nextWrite -= 1 # 마이너스 increment 순서 주의
				self.count -= 1
		
			self.nextWrite = 0
			self.nextPos = 0
			self.count = 0
		
		""" Get Position instance for this absolute position;
		this is allowed to be arbitrarily far "in the
		future" but cannot be before the last freeBefore. """
		def get(self, pos):
			# pos 는 increment 하게 증가하면서 들어온다.

			while pos >= self.nextPos:
				#print("count=" + count + " vs len=" + positions.length)
				if self.count == len(self.positions): # 같네? 그러면 늘려야지...
					#newPositions = []
					#for _ in range(0, self.count+1):
					#	newPositions.append(Position())

					self.newPositions = []
					for _ in range(0, 1+self.count):
						self.newPositions.append(KoreanTokenizer.Position())
		
					self.newPositions[:len(self.positions)-self.nextWrite] = self.positions[self.nextWrite:len(self.positions)-self.nextWrite]
					self.newPositions[len(self.positions)-self.nextWrite:self.nextWrite] = self.positions[:self.nextWrite]
					#for i in range(len(self.positions), len(self.newPositions)):
					#	newPositions[i] = Position()
					self.positions = self.newPositions[:]

				if self.nextWrite == len(self.positions):
					self.nextWrite = 0
				
				#print('self.nextWrite: ', self.nextWrite)
				#print('self.positions[self.nextWrite].count: ', self.positions[self.nextWrite].count)
				assert self.positions[self.nextWrite].count == 0
				
				self.positions[self.nextWrite].pos = self.nextPos
				self.nextWrite += 1
				self.nextPos += 1
				self.count += 1
		
			assert self.in_bounds(pos)
			index = self.get_index(pos)
			assert self.positions[index].pos == pos

			return self.positions[index] # Position() 클래스 출력
		
		def get_nextpos(self):
			return self.nextPos

		def in_bounds(self, pos):
			# For assert
			return pos < self.nextPos and pos >= self.nextPos - self.count
		
		def get_index(self, pos):
			index = self.nextWrite - (self.nextPos - pos)
			if index < 0:
				index += len(self.positions)
			return index
		
		# TODO: circular buffer 사용법 확인. (참고. circular buffer 사용하지 않음.)
		#def freeBefore(self, pos):
		#	toFree = self.count - (self.nextPos - pos)
		#	assert toFree >= 0
		#	assert toFree <= self.count
		#	index = self.nextWrite - self.count
		#	if index < 0:
		#		index += len(self.positions)
		#	for i in range(0, toFree):
		#		if index == len(self.positions):
		#			index = 0
		#		self.positions[index].reset()
		#		index += 1
		#	self.count -= toFree

	def compute_space_penalty(self, leftPOS, numSpaces):	
		""" Returns the space penalty associated with the provided POS.Tag.
		@params
			leftPOS - the left part of speech of the current token.
			numSpaces - the number of spaces before the current token.
   
		# mecab-ko-dic 의 dicrc 파일의 left-space-penality-factor 를 참조.
		# 좌측에 공백을 포함하는 품사의 연접 비용을 늘리기 위한 설정.
		"""
  
		spacePenalty = 0
		if numSpaces > 0:
			if leftPOS in [
       			'JKS', 'JKC', 'JKG', 'JKO', 'JKB', 'JKV', 'JKQ', 'JX', 'JC', # pos-id 120
				# TODO: pos-id 210
          		]:
				spacePenalty = 6000
			elif leftPOS in [
       				"EC", "EF", "EP", "ETM", "ETN", # pos-id 100
					'XSA', 'XSN', 'XSV' # 접미사 ; pos-id 183, 184, 185
           			'VCP', # pos-id 172; 긍정 지정사
					# TODO: pos-id 200, 220, 221, 222, 230
                ]:
				spacePenalty = 3000
		return spacePenalty

	def add(self, trie_dict, fromPosData, wordPos, endPos, wordID, type_, dict=None):
		"""Add the optimal token info to each position."""

		#leftPOS = dict.getLeftPOS(wordID)
		#wordCost = dict.getWordCost(wordID)
		#leftID = dict.getLeftId(wordID)
		leftPOS = trie_dict['POS']
		wordCost = trie_dict['word_cost']
		leftID = trie_dict['left_id']
		rightID = trie_dict['right_id']
		wordID = trie_dict['surface'] # wordID 가 원래 없지만, 그냥 surface로 사용하기로 하자.
		backPosType = trie_dict['POS_type']
		morphemes = trie_dict['morphemes']

		leastCost = sys.maxsize # Integer.MAX_VALUE
		leastIDX = -1
		assert fromPosData.count > 0
		
		for idx in range(0, fromPosData.count):
			# The number of spaces before the term
			numSpaces = wordPos - fromPosData.pos
	
			# Cost is path cost so far, plus word cost (added at end of loop), plus bigram cost and space penalty cost
			cost = fromPosData.costs[idx] \
				   + self.conn_costs.get(fromPosData.lastRightID[idx], leftID) \
				   + self.compute_space_penalty(leftPOS, numSpaces)

			if self.verbose:
				print("      fromIDX=" + str(idx) + ": cost=" + str(cost) + " (prevCost=" + str(fromPosData.costs[idx]) + " wordCost=" + str(wordCost) + " bgCost=" + str(self.conn_costs.get(fromPosData.lastRightID[idx], leftID)) +
            " spacePenalty=" + str(self.compute_space_penalty(leftPOS, numSpaces)) + ") leftID=" + str(leftID) + " leftPOS=" + leftPOS + ")")
			
			if cost < leastCost:
				leastCost = cost
				leastIDX = idx
				if self.verbose:
					print("        **")
		
		leastCost += wordCost
		
		if self.verbose:
			print("      + cost=" + str(leastCost) + " wordID=" + str(wordID) + " leftID=" + str(leftID) + " leastIDX=" + str(leastIDX) + " toPos=" + str(endPos) + " toPos.idx=" + str(self.positions.get(endPos).count))

		self.positions.get(endPos).add(cost=leastCost, lastRightID=rightID, 
									   backPos=fromPosData.pos, backRPos=wordPos, backIndex=leastIDX, backID=wordID, backDictType=type_, 
									   backPosType=backPosType, morphemes=morphemes, backPosTag=leftPOS)


	def increment_token(self):
		"""Excute parse() and save token info. until at the end of string.

		parse() is able to return w/o producing any new tokens,
		when the tokens it had produced were entirely punctuation.
		So we loop here until we get a real token or we end:
		"""
		while len(self.pending) == 0:

			if self.end:
				return False
		
			self.parse()
		
		token = self.pending.pop()
		length = token.getLength()
		assert length > 0
		
		self.tkn_attr_obj.termAtt.append(token.getSurfaceFormString())
		self.tkn_attr_obj.offsetAtt.append((token.getStartOffset(), token.getEndOffset()))
		#self.posAtt.tkn_attr.append(token)
		#self.readingAtt.tkn_attr.append(token)
		#self.tkn_attr_obj.posIncAtt.append(token.getPositionIncrement())
		self.tkn_attr_obj.posLengthAtt.append(token.getPositionLength())
		self.tkn_attr_obj.posTypeAtt.append(token.getPOSType())
		self.tkn_attr_obj.posTagAtt.append(token.getPOSTag())
		self.tkn_attr_obj.dictTypeAtt.append(token.getDictType())

		if self.verbose:
			print(":    incToken: return token= " + token.getSurfaceFormString())
		
		return True
	

	def parse(self):
		"""While move from start to end of input string in character unit, find the optimal path.

		Incrementally parse some more characters.  This runs
	    the viterbi search forwards "enough" so that we
	    generate some more tokens.  How much forward depends on
	    the chars coming in, since some chars could cause
	    longer-lasting ambiguity in the parsing.  Once the
	    ambiguity is resolved, then we back trace, produce
	    the pending tokens, and return
		"""

		if self.verbose:
			print("\nPARSE")
		
		# Index of the last character of unknown word:
		unknownWordEndIndex = -1
		
		# Maximum posAhead of user word in the entire input
		userWordMaxPosAhead = -1
		
		## Advances over each position (character):
		while True:

			if self.buffer.get(self.pos) == -1:
				# End
				break

			posData = self.positions.get(self.pos)
			isFrontier = self.positions.get_nextpos() == self.pos + 1	# boolean

			if posData.count == 0:
				# No arcs arrive here; move to next position:
				if self.verbose:
					print("    no arcs in; skip pos=" + str(self.pos))

				self.pos += 1
				continue

			if self.pos > self.last_backtrace_pos and posData.count == 1 and isFrontier:
				# 이 조건은 path 중의성이 없는 조건임. 
				# 따로 optimal path 찾을 필요가 없으므로 backtrace 실행.
				# backtrace 실행 후 다음 index부터 다시 parse 시작.
				# 완전한 viterbi 알고리즘이 아닌 이유.

				""" We are at a "frontier", and only one node is
				alive, so whatever the eventual best path is must
				come through this node.  So we can safely commit
				to the prefix of the best path at this point: """
				self.backtrace(posData, 0)

				# Re-base cost so we don't risk int overflow:
				posData.costs[0] = 0
				if len(self.pending) > 0:
					return
				else:
					""" This means the backtrace only produced
					punctuation tokens, so we must keep parsing	"""

			if self.pos - self.last_backtrace_pos >= self.MAX_BACKTRACE_GAP: # 예외처리.
				""" Safety: if we've buffered too much, force a
				backtrace now.  We find the least-cost partial
				path, across all paths, backtrace from it, and
				then prune all others.  Note that this, in
				general, can produce the wrong result, if the
				total best path did not in fact back trace
				through this partial best path.  But it's the
				best we can do... (short of not having a
				safety!). """

				""" First pass: find least cost partial path so far, 
				including ending at future positions: """
				leastIDX = -1
				leastCost = sys.maxsize

				""" TODO """
				# ...


			if self.verbose:
				print("\n  extend @ pos=" + str(self.pos) + " char=" + self.buffer.get(self.pos))
		
			if self.verbose:
				print("    " + str(posData.count) + " arcs in")

			""" Move to the first character that is not a whitespace.
			The whitespaces are added as a prefix for the term that we extract,
			this information is then used when computing the cost for the term using
			the space penalty factor.
			They are removed when the final tokens are generated. """

			if ord(self.buffer.get(self.pos)) in SPACE_SEPARATOR:
				# 보이는 것은 10진수(ex. 32)와 16진수(ex. 0x0020)가 다르지만, 프로그램 내부적으로는 integer형으로 같은 값을 가진다.
				# 따라서, if 0x0020 in [32, 64, 128, ...] 하면 True가 된다.
				# '0x%04x'%ord(self.buffer.get(self.pos)) 를 하면 16진수로 변환이 되나 문자열 타입이 된다. 따로 또 처리할 필요가 있음.

				self.pos += 1
				nextChar = self.buffer.get(self.pos)

				while nextChar != -1 and ord(nextChar) in SPACE_SEPARATOR:
					#print(self.pos)
					self.pos += 1
					nextChar = self.buffer.get(self.pos)
					
			if self.buffer.get(self.pos) == -1:
				self.pos = posData.pos

			anyMatches = False

			###
			## First try user dict:
			if self.user_dict is not None:
				#output = 0
				maxPosAhead = 0
				#outputMaxPosAhead = 0
				#arcFinalOutMaxPosAhead = 0

				posAhead = self.pos
				while True:
					
					ch = self.buffer.get(posAhead)

					if ch == -1:
						break

					_, userIdRef = self.user_dict.userTokenInfo.search(self.buffer.slice_get(self.pos, posAhead + 1))
					# 주의: [{'surface': '위메이크프라이스', 'left_id': 1781, 'right_id': 3534, 'word_cost': -100000, 'POS': 'NNG', 'POS_type': 'UnitTerm', 'analysis': '위메이크프라이스'}]
					# 리스트 안에 하나의 dict 들어가 있음을 주의!
					if userIdRef is not None: # Trie 결과는 항상 None 아니면 리스트 이다.
						maxPosAhead = posAhead
						#outputMaxPosAhead = output
						#arcFinalOutMaxPosAhead = arc.nextFinalOutput.intValue()
						lastResult = userIdRef[0] # 사용자 단어는 항상 유니크하므로 1개밖에 없다.
						anyMatches = True

					posAhead += 1

				# Longest matching for user word
				if anyMatches and maxPosAhead > userWordMaxPosAhead:
					if self.verbose:
						print("    USER word " + self.buffer.slice_get(self.pos, maxPosAhead + 1) + " toPos=" + str(maxPosAhead + 1))

					self.add(lastResult, posData, self.pos, maxPosAhead + 1, None, Type.USER)
					userWordMaxPosAhead = max(userWordMaxPosAhead, maxPosAhead)


			"""
		    TODO: we can be more aggressive about user
		    matches?  if we are "under" a user match then don't
		    extend KNOWN/UNKNOWN paths?
			"""
			if anyMatches == False:

				# Next, try known dictionary matches
				output = 0
				posAhead = self.pos

				while True:
					
					ch = self.buffer.get(posAhead)

					if ch == -1:
						break

					#print("    match " + ch + " posAhead=" + posAhead)	

			        # Optimization: for known words that are too-long
			        # (compound), we should pre-compute the 2nd
			        # best segmentation and store it in the
			        # dictionary instead of recomputing it each time a
			        # match is found.

					_, wordIdRef = self.kn_dict.sysTokenInfo.search(self.buffer.slice_get(self.pos, posAhead+1))
					if wordIdRef is not None:
						if self.verbose:
						#if True:
							print("    KNOWN word " + self.buffer.slice_get(self.pos, posAhead - self.pos + 1) + " toPos=" + str(posAhead + 1) + " " + str(len(wordIdRef)) + " wordIDs")
						for each in wordIdRef:
							self.add(each, posData, self.pos, posAhead+1, None, Type.KNOWN)
							anyMatches = True

					posAhead += 1

			if unknownWordEndIndex > posData.pos:
				self.pos += 1
				continue

			firstCharacter = self.buffer.get(self.pos)
			if anyMatches == False or self.character_definition.isInvoke(firstCharacter):

				# Find unknown match:
				characterId = self.character_definition.getCharacterClass(firstCharacter)
				# NOTE: copied from UnknownDictinary.lookup:
				if self.character_definition.isGroup(firstCharacter) == False:
					unknownWordLength = 1
				else:
					# Extract unknown word. Characters with the same script are considered to be part of unknown word
					unknownWordLength = 1
					# (ref) http://www.unicode.org/reports/tr24/#Data_File_SCX
					scriptCode = unicodedata.category(firstCharacter)
					isPunct = self.is_punctuation(firstCharacter)
					posAhead = self.pos + 1

					while True:
						
						next_ch = self.buffer.get(posAhead)
						#next_hex_ch = '0x%04x' % ord(next_ch)

						if next_ch == -1:
							break

						next_hex_ch = ord(next_ch)
						next_scriptCode = unicodedata.category(next_ch)

						if unknownWordLength == self.MAX_UNKNOWN_WORD_LENGTH:
							break

						sameScript = (scriptCode == next_scriptCode) or (next_hex_ch in NON_SPACING_MARK)
						if sameScript and isPunct == self.is_punctuation(next_ch) and self.character_definition.isGroup(next_ch):
							unknownWordLength += 1
						else:
							break

						posAhead += 1

				_, wordIdRef = self.unk_dict.unkTokenInfo.search(characterId)
				wordIdRef = wordIdRef[0] # unknown은 항상 1개 밖에 없다.
				if self.verbose:
					print("    UNKNOWN word len=" + str(unknownWordLength) + " " + str(len(wordIdRef)) + " wordIDs")
				self.add(wordIdRef, posData, self.pos, self.pos + unknownWordLength, None, Type.UNKNOWN)

			self.pos += 1
		self.end = True

		if self.pos > 0:
			endPosData = self.positions.get(self.pos)
			leastCost = sys.maxsize
			leastIDX = -1
			if self.verbose:
				print("  end: " + str(endPosData.count) + " nodes")

			for idx in range(0, endPosData.count):
				# Add EOS cost:
				cost = endPosData.costs[idx] + self.conn_costs.get(endPosData.lastRightID[idx], 0)

				if cost < leastCost:
					leastCost = cost
					leastIDX = idx

			self.backtrace(endPosData, leastIDX)

		else:
			# No characters in the input string; return no tokens!
			pass

	def backtrace(self, endPosData, fromIDX):
		"""Add tokens in the optimal path to the pending list while backtrace positions of input string.

		   the pending list.  The pending list is then in-reverse
		   (last token should be returned first).
		"""

		endPos = endPosData.pos

		if self.verbose:
			print("\n  backtrace: endPos=" + str(endPos) + " pos=" + str(self.pos) + "; " + str(self.pos - self.last_backtrace_pos) + " characters; last=" + str(self.last_backtrace_pos) + " cost=" + str(endPosData.costs[fromIDX]))

		fragment = self.buffer.slice_get(self.last_backtrace_pos, endPos)# - self.last_backtrace_pos)

		pos = endPos
		bestIDX = fromIDX

	    # TODO: sort of silly to make Token instances here; the
	    # back trace has all info needed to generate the
	    # token.  So, we could just directly set the attrs,
	    # from the backtrace, in increment_token w/o ever
	    # creating Token; we'd have to defer calling freeBefore
	    # until after the backtrace was fully "consumed" by
	    # increment_token.

		while pos > self.last_backtrace_pos:

			posData = self.positions.get(pos)
			assert bestIDX < posData.count

			backPos = posData.backPos[bestIDX]
			backWordPos = posData.backWordPos[bestIDX]
			assert backPos >= self.last_backtrace_pos

			length = pos - backWordPos
			backDictType = posData.backDictType[bestIDX]
			backID = posData.backID[bestIDX]
			nextBestIDX = posData.backIndex[bestIDX]

			#fragment = posData.backID[bestIDX]
			fragment = self.buffer.slice_get(backWordPos, backWordPos+length)
			backPosType = posData.backPosType[bestIDX]
			morphemes = posData.morphemes[bestIDX]
			backPosTag = posData.backPosTag[bestIDX]

			# the start of the word after the whitespace at the beginning
			fragmentOffset = backWordPos - self.last_backtrace_pos
			assert fragmentOffset >= 0

			xDict = self.get_dict(backDictType)


			if self.output_unknown_unigrams and backDictType == Type.UNKNOWN:
				# outputUnknownUnigrams converts unknown word into unigrams

				for i in range(length-1, -1, -1):
					
					charLen = 1
					
					# TODO: LowSurrogate 처리 (유니코드 상 2음절로 인식되는 캐릭터)
					# ...
					# if case:
					#     charLen = 2

					token = DictionaryToken(dictType=Type.UNKNOWN, dictionary=None, wordId=None, surfaceForm=fragment[i], 
											offset=fragmentOffset+i, length=charLen, startOffset=backWordPos+i, endOffset=backWordPos+i+charLen, 
											posType=backPosType, morphemes=morphemes, posTag=backPosTag) # 추가된 argument
					self.pending.append(token)
					if self.verbose:
						print(" (1)    add token=") # + self.pending[len(self.pending)-1])

			else:
				#print(backWordPos, backWordPos+length)
				#print(self.buffer.slice_get(backWordPos, backWordPos+length))
				token = DictionaryToken(dictType=backDictType, dictionary=None, wordId=None, surfaceForm=fragment, 
										offset=fragmentOffset, length=length, startOffset=backWordPos, endOffset=backWordPos+length, 
										posType=backPosType, morphemes=morphemes, posTag=backPosTag)


				#if token.getPOSType() == POS.Type.MORPHEME or self.mode == DcpdMode.NONE
				# POS.Type.MORPHEME 타입이라면? pass.
				# DcpdMode.NONE 모드라면? pass.
				if token.getPOSType() == POS.Type.MORPHEME or \
					(token.getPOSType() != POS.Type.INFLECT and self.mode == DcpdMode.NONE) or \
					(token.getPOSType() == POS.Type.INFLECT and self.infl_mode == DcpdMode.NONE):

							if self.should_filter_token(token) == False:
								self.pending.append(token)
								if self.verbose:
									print(" (2)    add token = ", token.getSurfaceFormString()) # + self.pending[len(self.pending)-1])

				#else:
				# POS.Type.MORPHEME 타입이라면? reject.
				# DcpdMode.NONE 모드라면? reject.
				# (주의. pass 와 pass 는 or로 결합. but, reject 과 reject의 결합은 and로 결합)
				if token.getPOSType() != POS.Type.MORPHEME and \
					(token.getPOSType() != POS.Type.INFLECT and self.mode != DcpdMode.NONE) or \
					(token.getPOSType() == POS.Type.INFLECT and self.infl_mode != DcpdMode.NONE):

					morphemes = token.getMorphemes() # sub words from compound noun
					if morphemes is None: # 이 경우는 거의 없음. 위에 'COMPOUND'를 알고 들어왔기에. 일종의 예외처리임.
						self.pending.append(token)
						if self.verbose:
							print(" (3)    add token = ", token.getSurfaceFormString()) # + self.pending[len(self.pending)-1])
					
					# 복합명사의 서브단어가 있는 경우.
					else: # sub words from compound noun
						endOffset = backWordPos + length
						posLen = 0
						# decompose the compound
						for i in range(len(morphemes)-1, -1, -1):
							morpheme = morphemes[i]

							if token.getPOSType() == POS.Type.COMPOUND:
								assert endOffset - len(morpheme.surfaceForm) >= 0
								#compoundToken = DecompoundToken(morpheme.posTag, morpheme.surfaceForm, endOffset - len(morpheme.surfaceForm), endOffset, backPosType, morphemes)
								compoundToken = DecompoundToken(posTag=morpheme.posTag, surfaceForm=morpheme.surfaceForm, 
																startOffset=endOffset-len(morpheme.surfaceForm), endOffset=endOffset, 
																posType=POS.Type.MORPHEME, dictType=backDictType)
							else:
								#compoundToken = DecompoundToken(morpheme.posTag, morpheme.surfaceForm, token.getStartOffset(), token.getEndOffset(), backPosType, morphemes)
								compoundToken = DecompoundToken(posTag=morpheme.posTag, surfaceForm=morpheme.surfaceForm, 
																#startOffset=endOffset-len(morpheme.surfaceForm), endOffset=endOffset,
																# INFLECT 은 원형, 서브단어의 offset이 꼬이므로 그냥 하나로 통일. 
																startOffset=token.getStartOffset(), endOffset=token.getEndOffset(), 
																posType=POS.Type.MORPHEME, dictType=backDictType)
							
							if i == 0 and \
								(self.mode == DcpdMode.MIXED or self.infl_mode == DcpdMode.MIXED):
								compoundToken.setPositionIncrement(0)
								
							# 서브단어 pending! (MIXED 와 DISCARD 모드만 실행)
							posLen += 1
							endOffset -= len(morpheme.surfaceForm)
							self.pending.append(compoundToken)
							if self.verbose:
								print(" (4)   add token = ", compoundToken.getSurfaceFormString()) # + self.pending[len(self.pending)-1])

						# 원형어 pending! (MIXED 모드만 실행)
						if (token.getPOSType() != POS.Type.INFLECT and self.mode == DcpdMode.MIXED) or \
							(token.getPOSType() == POS.Type.INFLECT and self.infl_mode == DcpdMode.MIXED):
							token.setPositionLength(max(1, posLen))
							self.pending.append(token) # 원형은 가장 마지막에 pending되어야 함. (bactrace 하기에)
							if self.verbose:
								print(" (5)   add token = ", token.getSurfaceFormString()) # + self.pending[len(self.pending)-1])
			
			# For Spacing
			if self.discard_punctuation == False and backWordPos != backPos:
				# Add a token for whitespaces between terms
				offset = backPos - self.last_backtrace_pos
				len_ = backWordPos - backPos
				_, wordIdRef = self.unk_dict.unkTokenInfo.search('SPACE')
				wordIdRef = wordIdRef[0]
				spaceToken = DictionaryToken(dictType=Type.UNKNOWN, dictionary=None, wordId=None, surfaceForm=' ', 
											 offset=offset, length=len_, startOffset=backPos, endOffset=backPos+len_, 
											 posType=POS.Type.MORPHEME, morphemes=None, posTag=wordIdRef['POS'])
				self.pending.append(spaceToken)

			pos = backPos
			bestIDX = nextBestIDX
			
		self.last_backtrace_pos = endPos		
		
		#if self.verbose:
		#	print("  freeBefore pos=" + str(endPos))
		
		# TODO: circular buffer 효용성 확인 필요.
		# Notify the circular buffers that we are done with these positions:
		# pynori는 circular buffer를 사용하지 않기에 아래의 과정은 필요 없다. 		
		#self.buffer.freeBefore(endPos);
		#self.positions.freeBefore(endPos); # 만약 실행 하면, self.last_backtrace_pos이 리셋되기 때문에, 포지션이 꼬임.
	
	def get_dict(self, type):
		if type == Type.USER:
			return self.user_dict
		elif type == Type.KNOWN:
			return self.kn_dict
		elif type == Type.UNKNOWN:
			return self.unk_dict

	def should_filter_token(self, token):
		"""Delete token, where the characters are 'all' punctuation.

		특수문자 토큰들을 제거하기 위한 함수 (ex. '.', '-', '#', '*', '---', '****' ...)
		사용자 사전 특징으로 특수문자가 포함된 토큰들이 사용자 단어로 지정될 수 있음 (ex. '안녕#', '!노리', '형#태소')

		self.user_dict.userTokenInfo.search() 함수를 사용해서 체크한 뒤, filter 여부 결정이 필요. (이상적인 구현임)
		(그러나) 사용자 사전에 특수문자가 포함된 토큰들이 없다면, 위 userTokenInfo.search() 함수 사용은 속도 저하를 일으킴.
		(따라서) 단순하게, 아래와 같이 '하나라도 punctuation'이 있으면 삭제하는 방법 선택.

		사용자 사전 작성 정책에 따라서 (특수문자 부분) 이 함수의 정의가 달라져야 함.
		"""
		is_punct = True
		for ch in token.getSurfaceForm(): # 토큰 전체를 대상으로 탐색.
			if self.is_punctuation(ch) == False: # 하나라도 punctuation이 있으면 삭제.
				is_punct = False
				return self.discard_punctuation and is_punct
		return self.discard_punctuation and is_punct
		#return self.is_punctuation(token.getSurfaceForm()[token.getOffset()])

	def is_punctuation(self, ch):
		#hex_ch = '0x%04x' % ord(ch)
		hex_ch = ord(ch)	
		if hex_ch == 0x318d: # 'ㆍ'
			return True
		if hex_ch in SPACE_SEPARATOR or \
		   hex_ch in LINE_SEPARATOR or \
		   hex_ch in PARAGRAPH_SEPARATOR or \
		   hex_ch in CONTROL or \
		   hex_ch in FORMAT or \
		   hex_ch in DASH_PUNCTUATION or \
		   hex_ch in START_PUNCTUATION or \
		   hex_ch in END_PUNCTUATION or \
		   hex_ch in CONNECTOR_PUNCTUATION or \
		   hex_ch in OTHER_PUNCTUATION or \
		   hex_ch in MATH_SYMBOL or \
		   hex_ch in CURRENCY_SYMBOL or \
		   hex_ch in MODIFIER_SYMBOL or \
		   hex_ch in OTHER_SYMBOL or \
		   hex_ch in INITIAL_QUOTE_PUNCTUATION or \
		   hex_ch in FINAL_QUOTE_PUNCTUATION:
			return True
		return False
		
