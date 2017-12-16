#!/usr/bin/env python2.7
from __future__ import division
import rospy
import sys, select, os
import numpy as np
import time

from std_msgs.msg import String
from nav_msgs.msg import OccupancyGrid
from geometry_msgs.msg import PoseStamped, Pose, Point, Quaternion, Pose2D
from visualization_msgs.msg import Marker, MarkerArray
from crazyflie_human.msg import OccupancyGridTime, ProbabilityGrid

# Get the path of this file, go up two directories, and add that to our 
# Python path so that we can import the pedestrian_prediction module.
sys.path.append(os.path.dirname(os.path.realpath(__file__)) + "/../../")

from pedestrian_prediction.pp.mdp import GridWorldMDP
from pedestrian_prediction.pp.inference import hardmax as inf
from pedestrian_prediction.pp.plot import plot_heat_maps

A = GridWorldMDP.Actions

class HumanPrediction(object):
	"""
	This class models and predicts human motions in a 2D planar environment.
	It stores:
		- human's tracked trajectory 
		- occupancy grid of states human is likely to go to
		- moving obstacle representing human future motion
	"""

	def __init__(self):

		# create ROS node
		rospy.init_node('human_prediction', anonymous=True)

		# load all the prediction params and setup subscriber/publishers
		self.load_parameters()
		self.register_callbacks()

		rate = rospy.Rate(100) 

		while not rospy.is_shutdown():
			if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
				line = raw_input()
				break

			# TODO this is only for 1 goal right now
			# plot start/goal markers for visualization
			self.start_pub.publish(self.state_to_marker(xy=self.real_start, color="G"))
			self.goal_pub.publish(self.state_to_marker(xy=self.real_goals[0], color="R"))

			rate.sleep()

	def load_parameters(self):
		"""
		Loads all the important paramters of the human sim
		"""
		# --- simulation params ---# 

		# measurements of gridworld
		self.sim_height = int(rospy.get_param("pred/sim_height"))
		self.sim_width = int(rospy.get_param("pred/sim_width"))

		# start and goal locations 
		self.sim_start = rospy.get_param("pred/sim_start")
		self.sim_goals = rospy.get_param("pred/sim_goals")

		# resolution (m/cell)
		self.res = rospy.get_param("pred/resolution")

		# simulation forward prediction parameters
		self.fwd_tsteps = rospy.get_param("pred/fwd_tsteps")
		self.fwd_deltat = rospy.get_param("pred/fwd_deltat")

		# rationality coefficient
		self.beta = rospy.get_param("pred/init_beta")

		self.human_height = rospy.get_param("pred/human_height")
		self.prob_thresh = rospy.get_param("pred/prob_thresh")	

		# stores 2D array of size (fwd_tsteps) x (height x width) of probabilities
		self.occupancy_grids = None

		# grid world representing the experimental environment
		self.gridworld = GridWorldMDP(self.sim_height, self.sim_width, {}, default_reward=-4)

		# --- real-world params ---# 

		low = rospy.get_param("state/lower")
		up = rospy.get_param("state/upper")

		# get real-world measurements of experimental space
		self.real_height = up[1] - low[1] 
		self.real_width = up[0] - low[0] 

		# start and goal locations 
		self.real_start = self.sim_to_real_coord(self.sim_start)
		self.real_goals = [self.sim_to_real_coord(g) for g in self.sim_goals]

		# tracks the human's state over time
		self.human_traj = None

		# set start time to None until get first human state message
		self.start_t = None

	#TODO THESE TOPICS SHOULD BE FROM THE YAML/LAUNCH FILE
	def register_callbacks(self):
		"""
		Sets up all the publishers/subscribers needed.
		"""
		# subscribe to the info of the human walking around the space
		self.human_sub = rospy.Subscriber('/human_pose', PoseStamped, 
											self.human_state_callback, queue_size=1)

		# occupancy grid publisher & small publishers for visualizing the start/goal
		self.occu_pub = rospy.Publisher('/occupancy_grid_time', OccupancyGridTime, queue_size=1)
		self.goal_pub = rospy.Publisher('/goal_marker', Marker, queue_size=10)
		self.start_pub = rospy.Publisher('/start_marker', Marker, queue_size=10)
		self.grid_vis_pub = rospy.Publisher('/grid_vis_marker', Marker, queue_size=10)

	# ---- Inference Functionality ---- #

	def human_state_callback(self, msg):
		"""
		Grabs the human's state from the mocap publisher
		"""
		# update the map with where the human is at the current time
		self.update_human_traj([msg.pose.position.x, msg.pose.position.y])
	
		# infer the new human occupancy map from the current state
		self.infer_occupancies() 

		# publish occupancy grid list
		self.occu_pub.publish(self.grid_to_message())

		# TODO THIS IS DEBUG
		self.visualize_occugrid(1.5)
		#self.visualize_occugrid(2)
		#self.visualize_occugrid(3)
		#self.visualize_occugrid(3.8)

	def update_human_traj(self, newstate):
		"""
		Given a new sensor measurement of where the human is, update the tracked
		trajectory of the human's movements.
		"""
		if self.human_traj is None:
			self.human_traj = np.array([newstate])
		else:
			self.human_traj = np.append(self.human_traj, np.array([newstate]), 0)

	# TODO we need to have beta updated over time, and have a beta for each goal
	def infer_occupancies(self):
		"""
		Using the current trajectory data, recompute a new occupancy grid
		for where the human might be
		"""
		if self.human_traj is None:
			print "Can't infer occupancies -- human hasn't appeared yet!"
			return 

		# TODO update beta here

		sim_coord = self.real_to_sim_coord(self.human_traj[-1])
		#print "(real) human traj latest: " + str(self.human_traj[-1])
		#print "(sim) corrected human traj: ", corrected

		goal = self.sim_goals[0]		# TODO only for one goal

		curr_state = self.gridworld.coor_to_state(sim_coord[0], sim_coord[1])
		curr_goal = self.gridworld.coor_to_state(goal[0], goal[1])	

		# returns all state probabilities for timesteps 0,1,...,T in a 2D array. 
		# (with dimension (T+1) x (height x width)
		self.occupancy_grids = inf.state.infer_from_start(self.gridworld, curr_state,
									curr_goal, T=self.fwd_tsteps, beta=self.beta, all_steps=True)

	# ---- Utility Functions ---- #
	
	def sim_to_real_coord(self, sim_coord):
		"""
		Takes [x,y] coordinate in simulation frame and returns a shifted
		value in the ROS coordinates
		"""
		return [sim_coord[0]*self.res - (self.real_width/2.0), 
						sim_coord[1]*self.res - (self.real_height/2.0)]

	def real_to_sim_coord(self, real_coord):
		"""
		Takes [x,y] coordinate in the ROS real frame, and returns a shifted
		value in the simulation frame
		"""
		return [int((real_coord[0]+self.real_width/2.0)/self.res), 
						int((real_coord[1]+self.real_height/2.0)/self.res)]

	def interpolate_grid(self, future_time):
		"""
		Interpolates the grid at some future time
		"""
		if self.occupancy_grids is None:
			print "Occupancy grids are not created yet!"
			return None

		if future_time < 0:
			print "Can't interpolate for negative time!"
			return None

		if future_time > self.fwd_tsteps:
			print "Can't interpolate more than", self.fwd_tsteps, "steps into future!"
			print "future_time =", future_time
			return None

		in_idx = -1
		for i in range(self.fwd_tsteps+1):
			if np.isclose(i, future_time, rtol=1e-05, atol=1e-08):
				in_idx = i
				break
	
		if in_idx != -1:
			# if interpolating exactly at the timestep
			return self.occupancy_grids[in_idx]
		else:
			prev_t = int(future_time)
			next_t = int(future_time)+1

			low_grid = self.occupancy_grids[prev_t]
			high_grid = self.occupancy_grids[next_t]

			interpolated_grid = np.zeros((self.sim_height*self.sim_width))

			for i in range(self.sim_height*self.sim_width):
				prev = low_grid[i]
				next = high_grid[i]
				curr = prev + (next - prev) *((future_time - prev_t) / (next_t - prev_t))
				if curr > self.prob_thresh:
					print "At location", self.gridworld.state_to_coor(i)
					print "(prev_t, next_t): ", (prev_t, next_t)
					print "prev: ", prev
					print "next: ", next
					print "curr: ", curr
				interpolated_grid[i] = curr

			return interpolated_grid
			
	# ---- ROS Message Conversion ---- #

	def grid_to_message(self):
		"""
		Converts OccupancyGridTime structure to ROS msg
		"""
		timed_grid = OccupancyGridTime()
		timed_grid.gridarray = [None]*self.fwd_tsteps

		curr_time = rospy.Time.now()

		# set the start time of the experiment to the time of first occugrid msg
		if self.start_t is None:
			self.start_t = curr_time.secs

		for t in range(self.fwd_tsteps):
			grid_msg = ProbabilityGrid()

			# Set up the header.
			grid_msg.header.stamp = curr_time + rospy.Duration(t*self.fwd_deltat)
			grid_msg.header.frame_id = "/world"

			# .info is a nav_msgs/MapMetaData message. 
			grid_msg.resolution = self.res
			grid_msg.width = self.real_width
			grid_msg.height = self.real_height

			# Rotated maps are not supported... 
			origin_x=0.0 
			origin_y=0.0 
			grid_msg.origin = Pose(Point(origin_x, origin_y, 0), Quaternion(0, 0, 0, 1))

			# convert to list of doubles from 0-1
			grid_msg.data = list(self.occupancy_grids[t])

			timed_grid.gridarray[t] = grid_msg
 
		return timed_grid

	def state_to_marker(self, xy=[0,0], color="R"):
		"""
		Converts xy position to marker type to vizualize human
		"""
		marker = Marker()
		marker.header.frame_id = "/world"

		marker.type = marker.SPHERE
		marker.action = marker.ADD
		marker.pose.orientation.w = 1
		marker.pose.position.z = 0
		marker.scale.x = self.res
		marker.scale.y = self.res
		marker.scale.z = self.res
		marker.color.a = 1.0
		if color is "R":
			marker.color.r = 1.0
		elif color is "G":
			marker.color.g = 1.0
		else:
			marker.color.b = 1.0

		marker.pose.position.x = xy[0]
		marker.pose.position.y = xy[1]

		return marker

	def visualize_occugrid(self, time):
		"""
		Visualizes occupancy grid at time
		"""
		marker = Marker()
		marker.header.frame_id = "/world"
		marker.header.stamp = rospy.Time.now()
		marker.id = 0

		marker.type = marker.CUBE
		marker.action = marker.ADD

		marker.scale.x = self.real_width
		marker.scale.y = self.real_height
		marker.scale.z = self.real_width/2.0
		marker.color.a = 0.3
		marker.color.r = 0.3
		marker.color.g = 0.7
		marker.color.b = 0.7

		marker.pose.orientation.w = 1
		marker.pose.position.z = 0
		marker.pose.position.x = -self.real_height/2.0+0.5*marker.scale.x 
		marker.pose.position.y = -self.real_width/2.0+0.5*marker.scale.y
		marker.pose.position.z = 0.0+0.5*marker.scale.z

		self.grid_vis_pub.publish(marker)

		if self.occupancy_grids is not None:
			grid = self.interpolate_grid(time)
		
			if grid is not None:
				for i in range(len(grid)):
					# only visualize if greater than prob thresh
					if grid[i] > self.prob_thresh:
						(row, col) = self.gridworld.state_to_coor(i)
						real_coord = self.sim_to_real_coord([row, col])

						marker = Marker()
						marker.header.frame_id = "/world"
						marker.header.stamp = rospy.Time.now()
						marker.id = i+1

						marker.type = marker.CUBE
						marker.action = marker.ADD

						marker.scale.x = 1
						marker.scale.y = 1
						marker.scale.z = self.human_height
						marker.color.a = 0.4
						marker.color.r = 1
						marker.color.g = 1 - grid[i]
						marker.color.b = 0

						marker.pose.orientation.w = 1
						marker.pose.position.z = 0
						marker.pose.position.x = real_coord[0]
						marker.pose.position.y = real_coord[1]
						marker.pose.position.z = 2

						self.grid_vis_pub.publish(marker)

if __name__ == '__main__':

	human = HumanPrediction()
